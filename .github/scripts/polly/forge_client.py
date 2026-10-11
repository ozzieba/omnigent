"""The eight Polly gh operations on the personal Forge API.

Root must bind the existing Actions identity to a reviewed probe receipt first.
This module does not select an identity from a login, role, or invented bot field.
"""

import http.client
import json
import os
import re
import signal
import ssl
import stat
import sys
import time
from urllib.parse import urlsplit

ORIGIN = "https://forge.ozba.me"
REPOSITORY = "oz/omnigent"
REPO_ID = 11
PREFIX = "/api/v1/repos/oz/omnigent"
FORGE_ACTIONS_BINDING = {
    "actor_id": -2,
    "actor_login": "forgejo-actions",
    "receipt_sha256": "b0dfae19dfad8993d2368dc6305a55daa090f882de31da84cb9580238754f6b1",
    "repo_id": 11,
    "repository": "oz/omnigent",
}
JSON_LIMIT = 2 * 1024 * 1024
DIFF_LIMIT = 8 * 1024 * 1024
COMMENT_LIMIT = 65536
TOTAL_SECONDS = 25
REQUEST_SECONDS = 5
BOT_QUERY = '.[] | select(.user.type == "Bot") | [.html_url, (.body | gsub("\\n"; " "))] | @tsv'
VIEW_FIELDS = "title,body,baseRefName,headRefName,additions,deletions,changedFiles"


class Refusal(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(value, code):
    if not value:
        raise Refusal(code)


def binding_checked(binding):
    require(type(binding) is dict, "ACTIONS_IDENTITY_UNBOUND")
    require(
        set(binding) == {"actor_id", "actor_login", "receipt_sha256", "repo_id", "repository"},
        "BINDING_SHAPE",
    )
    require(type(binding["actor_id"]) is int and binding["actor_id"] != 0, "BINDING_ACTOR")
    require(
        type(binding["actor_login"]) is str
        and re.fullmatch(r"[A-Za-z0-9_.\[\]-]{1,100}", binding["actor_login"]),
        "BINDING_ACTOR",
    )
    # Forge's version-bound Actions system user is distinct from human users.
    require(
        binding["actor_id"] == -2 and binding["actor_login"] == "forgejo-actions",
        "ACTIONS_ROLE_UNQUALIFIED",
    )
    require(
        type(binding["receipt_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", binding["receipt_sha256"]),
        "BINDING_RECEIPT",
    )
    require(
        type(binding["repo_id"]) is int
        and binding["repo_id"] == REPO_ID
        and binding["repository"] == REPOSITORY,
        "BINDING_REPOSITORY",
    )
    return dict(binding)


def number_checked(value):
    require(type(value) is str and re.fullmatch(r"[1-9][0-9]{0,9}", value), "PR_NUMBER")
    return value


def parse_operation(argv):
    if len(argv) >= 2 and argv[0] == "api":
        require(argv[1].startswith("repos/oz/omnigent/"), "OWN_REPOSITORY_PATH")
        suffix = "/" + argv[1][len("repos/oz/omnigent/") :]
        pull = re.fullmatch(r"/pulls/([1-9][0-9]{0,9})", suffix)
        comments = re.fullmatch(r"/issues/([1-9][0-9]{0,9})/comments", suffix)
        reaction = re.fullmatch(r"/issues/comments/([1-9][0-9]{0,9})/reactions", suffix)
        if reaction and argv[2:] == ["-f", "content=eyes", "--silent"]:
            return "reaction", reaction[1], None
        if pull and argv[2:] == ["--jq", ".head.sha"]:
            return "head", pull[1], None
        if pull and argv[2:] == ["--jq", '.author_association // "NONE"']:
            return "association", pull[1], None
        if pull and argv[2:] == ["-H", "Accept: application/vnd.github.v3.diff"]:
            return "diff", pull[1], None
        if (
            comments
            and len(argv) == 5
            and argv[2:4] == ["--paginate", "--jq"]
            and " ".join(argv[4].split()) == BOT_QUERY
        ):
            return "comments", comments[1], None
    if len(argv) == 7 and argv[:2] in (["pr", "view"], ["pr", "comment"]):
        require(argv[3:5] == ["--repo", REPOSITORY], "OWN_REPOSITORY_PATH")
        number = number_checked(argv[2])
        if argv[1] == "view" and argv[5:] == ["--json", VIEW_FIELDS]:
            return "view", number, None
        if argv[1] == "comment" and argv[5] == "--body-file":
            return "comment", number, argv[6]
    raise Refusal("UNSUPPORTED_SITE")


def read_body(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode) and 0 < before.st_size <= COMMENT_LIMIT, "COMMENT_FILE"
        )
        raw = bytearray()
        while len(raw) <= COMMENT_LIMIT:
            part = os.read(fd, min(8192, COMMENT_LIMIT + 1 - len(raw)))
            if not part:
                break
            raw.extend(part)
        after = os.fstat(fd)

        def identity(item):
            return (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )

        require(
            identity(before) == identity(after) and len(raw) == before.st_size,
            "COMMENT_FILE_CHANGED",
        )
        return raw.decode("utf-8")
    finally:
        os.close(fd)


def json_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "JSON_DUPLICATE_KEY")
        result[key] = value
    return result


class Transport:
    def __init__(self, token, connection_factory=None, clock=time.monotonic):
        require(
            type(token) is str
            and 0 < len(token) <= 8192
            and all(32 < ord(c) < 127 for c in token),
            "ACTIONS_TOKEN",
        )
        self.token = token
        self.clock = clock
        self.end = clock() + TOTAL_SECONDS
        self.connection_factory = connection_factory or self.connection

    @staticmethod
    def connection(timeout):
        return http.client.HTTPSConnection(
            "forge.ozba.me", 443, timeout=timeout, context=ssl.create_default_context()
        )

    def remaining(self):
        remaining = self.end - self.clock()
        require(remaining > 0, "TRANSPORT_DEADLINE")
        return min(REQUEST_SECONDS, remaining)

    def request(self, method, path, payload=None, text=False):
        require(
            method in ("GET", "POST")
            and path.startswith("/api/v1/")
            and not any(c in path for c in ("\r", "\n", "#", "\\")),
            "TRANSPORT_PATH",
        )
        limit = DIFF_LIMIT if text else JSON_LIMIT
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        )
        require(body is None or len(body) <= COMMENT_LIMIT + 1024, "REQUEST_BODY_BOUND")
        conn = self.connection_factory(self.remaining())
        sent = False
        try:
            conn.connect()
            conn.sock.settimeout(self.remaining())
            headers = {
                "Authorization": "token " + self.token,
                "Accept": "text/plain" if text else "application/json",
                "Accept-Encoding": "identity",
            }
            if body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            sent = True
            conn.request(method, path, body=body, headers=headers)
            conn.sock.settimeout(self.remaining())
            response = conn.getresponse()
            response_socket = response.fp.raw._sock
            self.remaining()
            require(
                response.status in ((200, 201) if method == "POST" else (200,)), "HTTP_REFUSED"
            )
            pairs = response.getheaders()
            keys = [key.lower() for key, _ in pairs]
            for key in (
                "content-length",
                "content-type",
                "content-encoding",
                "x-total-count",
                "link",
            ):
                require(keys.count(key) <= 1, "DUPLICATE_HEADER")
            headers = {key.lower(): value for key, value in pairs}
            require(
                headers.get("content-encoding", "identity").lower() == "identity",
                "CONTENT_ENCODING",
            )
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            require(content_type == ("text/plain" if text else "application/json"), "CONTENT_TYPE")
            declared = headers.get("content-length")
            if declared is not None:
                require(
                    re.fullmatch(r"[0-9]+", declared) and int(declared) <= limit,
                    "RESPONSE_BODY_BOUND",
                )
            raw = bytearray()
            while True:
                response_socket.settimeout(self.remaining())
                chunk = response.read1(min(8192, limit + 1 - len(raw)))
                self.remaining()
                raw.extend(chunk)
                require(len(raw) <= limit, "RESPONSE_BODY_BOUND")
                if not chunk:
                    break
            require(declared is None or len(raw) == int(declared), "RESPONSE_TRUNCATED")
            decoded = raw.decode("utf-8")
            value = (
                decoded
                if text
                else json.loads(
                    decoded,
                    object_pairs_hook=json_object,
                    parse_constant=lambda _: (_ for _ in ()).throw(Refusal("JSON_CONSTANT")),
                )
            )
            self.remaining()
            return value, headers
        except Exception as error:
            if method == "POST" and sent:
                raise Refusal("POST_OUTCOME_UNKNOWN_NO_RETRY") from None
            if isinstance(error, Refusal):
                raise
            raise Refusal("TRANSPORT_OR_JSON_REFUSED") from None
        finally:
            conn.close()


class ForgeClient:
    def __init__(self, binding, transport):
        self.binding = binding_checked(binding)
        self.transport = transport

    def admit(self):
        user, _ = self.transport.request("GET", "/api/v1/user")
        require(
            type(user) is dict
            and type(user.get("id")) is int
            and user["id"] == self.binding["actor_id"]
            and user.get("login") == self.binding["actor_login"],
            "ACTIONS_IDENTITY_DRIFT",
        )
        repo, _ = self.transport.request("GET", PREFIX)
        require(
            type(repo) is dict
            and type(repo.get("id")) is int
            and repo["id"] == REPO_ID
            and repo.get("full_name") == REPOSITORY,
            "REPOSITORY_IDENTITY_DRIFT",
        )

    def pull(self, number):
        value, _ = self.transport.request("GET", PREFIX + "/pulls/" + number)
        require(
            type(value) is dict
            and type(value.get("number")) is int
            and value["number"] == int(number),
            "PR_IDENTITY",
        )
        return value

    def comments(self, number):
        rows, ids = [], set()
        total = None
        advertised_mode = None
        for page in range(1, 5):
            value, headers = self.transport.request(
                "GET", PREFIX + "/issues/" + number + "/comments?limit=50&page=" + str(page)
            )
            require(type(value) is list and len(value) <= 50, "COMMENT_SHAPE")
            advertised = headers.get("x-total-count")
            present = advertised is not None
            if advertised_mode is None:
                advertised_mode = present
            require(present == advertised_mode, "COMMENT_TOTAL_MISSING_OR_APPEARED")
            if present:
                require(re.fullmatch(r"[0-9]+", advertised), "COMMENT_TOTAL")
                count = int(advertised)
                require(
                    count <= 200 and (total is None or total == count),
                    "COMMENT_TOTAL_DRIFT_OR_BOUND",
                )
                total = count
            for row in value:
                require(
                    type(row) is dict
                    and type(row.get("id")) is int
                    and row["id"] > 0
                    and row["id"] not in ids,
                    "COMMENT_IDENTITY",
                )
                require(
                    type(row.get("body")) is str
                    and len(row["body"].encode("utf-8")) <= COMMENT_LIMIT,
                    "COMMENT_BODY",
                )
                ids.add(row["id"])
                rows.append(row)
            require(total is None or len(rows) <= total, "COMMENT_OVERCOUNT")
            link = headers.get("link", "")
            relations = re.findall(r'rel\s*=\s*(?:"([^"]*)"|([^;,\s]+))', link, re.I)
            next_page = any(
                "next" in (quoted or bare).lower().split() for quoted, bare in relations
            )
            if len(value) < 50 or (total is not None and len(rows) == total):
                require(
                    not next_page and (total is None or len(rows) == total), "COMMENT_COVERAGE"
                )
                result = []
                for row in rows:
                    user = row.get("user")
                    if (
                        type(user) is not dict
                        or type(user.get("id")) is not int
                        or user["id"] != self.binding["actor_id"]
                    ):
                        continue
                    require(
                        user.get("login") == self.binding["actor_login"], "COMMENT_ACTOR_DRIFT"
                    )
                    url = row.get("html_url")
                    require(type(url) is str, "COMMENT_URL")
                    parsed = urlsplit(url)
                    require(
                        parsed.scheme == "https"
                        and parsed.netloc == "forge.ozba.me"
                        and parsed.path
                        in ("/oz/omnigent/pulls/" + number, "/oz/omnigent/issues/" + number)
                        and not parsed.query
                        and parsed.fragment == "issuecomment-" + str(row["id"]),
                        "COMMENT_URL",
                    )
                    result.append(row)
                return result
        raise Refusal("COMMENT_PAGE_BOUND")

    def dispatch(self, operation, body_reader=read_body):
        action, number, path = operation
        self.admit()
        if action == "reaction":
            self.transport.request(
                "POST", PREFIX + "/issues/comments/" + number + "/reactions", {"content": "eyes"}
            )
            return ""
        if action == "comments":

            def tsv(value):
                return (
                    value.replace("\\", "\\\\")
                    .replace("\t", "\\t")
                    .replace("\r", "\\r")
                    .replace("\n", "\\n")
                )

            return "".join(
                tsv(row["html_url"]) + "\t" + tsv(row["body"].replace("\n", " ")) + "\n"
                for row in self.comments(number)
            )
        if action == "diff":
            return self.transport.request("GET", PREFIX + "/pulls/" + number + ".diff", text=True)[
                0
            ]
        if action == "comment":
            self.transport.request(
                "POST", PREFIX + "/issues/" + number + "/comments", {"body": body_reader(path)}
            )
            return ""
        pull = self.pull(number)
        if action == "head":
            head = pull.get("head", {}).get("sha")
            require(type(head) is str and re.fullmatch(r"[0-9a-f]{40}", head), "HEAD_SHA")
            return head + "\n"
        if action == "association":
            # Forge's qualified PullRequest schema has no association field.
            return "NONE\n"
        require(action == "view", "UNSUPPORTED_SITE")
        require(
            type(pull.get("title")) is str
            and (pull.get("body") is None or type(pull.get("body")) is str),
            "PR_SHAPE",
        )
        for name in ("additions", "deletions", "changed_files"):
            require(type(pull.get(name)) is int and pull[name] >= 0, "PR_SHAPE")
        for branch in ("head", "base"):
            require(
                type(pull.get(branch)) is dict and type(pull[branch].get("ref")) is str, "PR_SHAPE"
            )
        return (
            json.dumps(
                {
                    "title": pull["title"],
                    "body": pull.get("body") or "",
                    "baseRefName": pull["base"]["ref"],
                    "headRefName": pull["head"]["ref"],
                    "additions": pull["additions"],
                    "deletions": pull["deletions"],
                    "changedFiles": pull["changed_files"],
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def main(argv):
    timer_installed = False
    previous_handler = None
    try:
        require(
            os.environ.get("GITHUB_SERVER_URL") == ORIGIN
            and os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
            "PLATFORM_OR_REPOSITORY",
        )
        binding = binding_checked(FORGE_ACTIONS_BINDING)
        if argv == ["--check-binding"]:
            return 0
        operation = parse_operation(argv)

        # Socket timeouts alone cannot bound a peer that dribbles header bytes.
        def expire(_signum, _frame):
            raise Refusal("TRANSPORT_DEADLINE")

        require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "EXISTING_TIMER")
        previous_handler = signal.signal(signal.SIGALRM, expire)
        signal.setitimer(signal.ITIMER_REAL, TOTAL_SECONDS)
        timer_installed = True
        client = ForgeClient(binding, Transport(os.environ.get("GH_TOKEN", "")))
        sys.stdout.write(client.dispatch(operation))
        return 0
    except Refusal as error:
        sys.stderr.write("POLLY_PLATFORM_REFUSED:" + error.code + "\n")
        return 2
    except Exception:  # noqa: BLE001 -- Keep tokens and response bodies out of errors.
        sys.stderr.write("POLLY_PLATFORM_REFUSED:LOCAL_INPUT_OR_SCHEMA\n")
        return 2
    finally:
        if timer_installed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
