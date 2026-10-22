"""Polly and the trusted UI gate gh operations on the personal Forge API.

Root must bind the existing Actions identity to a reviewed probe receipt first.
This module does not select an identity from a login, role, or invented bot field.
"""

import base64
import http.client
import json
import os
import re
import signal
import ssl
import stat
import sys
import time
from datetime import datetime
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
FILES_QUERY = ".[] | [.status, .filename] | @tsv"
LABEL_QUERY = '[.labels[].name] | index("skip-e2e-ui-test") != null'
REVIEW_QUERY = (
    '[.[] | select(.state != "COMMENTED")] | group_by(.user.login) '
    '| map(max_by(.submitted_at)) | .[] | select(.state == "APPROVED") | .user.login'
)


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
    gate = parse_gate_operation(argv)
    if gate is not None:
        return gate
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


def parse_gate_operation(argv):
    if argv == [
        "api",
        "repos/oz/omnigent/contents/.github/MAINTAINER?ref=main",
        "--jq",
        ".content",
    ]:
        return "gate-maintainers", None, None
    if len(argv) >= 2 and argv[0] == "api":
        match = re.fullmatch(
            r"repos/oz/omnigent/pulls/([1-9][0-9]{0,9})(/files|/reviews)?", argv[1]
        )
        if match:
            number, suffix = match.groups()
            tail = argv[2:]
            if suffix == "/files" and tail == ["--paginate"]:
                return "gate-files-json", number, None
            if suffix == "/files" and tail == ["--paginate", "--jq", FILES_QUERY]:
                return "gate-files-tsv", number, None
            if suffix == "/reviews" and tail == ["--paginate", "--jq", REVIEW_QUERY]:
                return "gate-approvers", number, None
            if suffix is None and tail == ["--jq", LABEL_QUERY]:
                return "gate-label", number, None
    if len(argv) == 9 and argv[:2] == ["pr", "view"] and argv[3:5] == ["--repo", REPOSITORY]:
        number = number_checked(argv[2])
        if argv[5:] == ["--json", "title", "--jq", ".title"]:
            return "gate-title", number, None
        if argv[5:] == ["--json", "author", "--jq", ".author.login"]:
            return "gate-author", number, None
    return None


def paginated_gate_rows(transport, path, key):
    rows, seen, total = [], set(), None
    for page in range(1, 5):
        value, headers = transport.request("GET", path + "?limit=50&page=" + str(page))
        require(type(value) is list and len(value) <= 50, "GATE_PAGE_SHAPE")
        advertised = headers.get("x-total-count")
        require(
            type(advertised) is str and re.fullmatch(r"[0-9]+", advertised),
            "GATE_TOTAL_REQUIRED",
        )
        count = int(advertised)
        require(
            count <= 200 and (total is None or total == count),
            "GATE_TOTAL_DRIFT_OR_BOUND",
        )
        total = count
        for row in value:
            require(type(row) is dict and key in row, "GATE_ROW_SHAPE")
            ident = row[key]
            require(
                type(ident) is (str if key == "filename" else int) and ident not in seen,
                "GATE_ROW_IDENTITY",
            )
            seen.add(ident)
            rows.append(row)
        require(len(rows) <= total, "GATE_OVERCOUNT")
        relations = re.findall(
            r'rel\s*=\s*(?:"([^"]*)"|([^;,\s]+))', headers.get("link", ""), re.I
        )
        has_next = any("next" in (quoted or bare).lower().split() for quoted, bare in relations)
        if len(rows) == total:
            require(not has_next, "GATE_NEXT_AFTER_TOTAL")
            return rows
        require(len(value) == 50, "GATE_SHORT_PAGE")
    raise Refusal("GATE_PAGE_BOUND")


def git_quoted_path(value):
    data = value.encode("utf-8")
    escaped = {
        7: r"\a",
        8: r"\b",
        9: r"\t",
        10: r"\n",
        11: r"\v",
        12: r"\f",
        13: r"\r",
        34: r"\"",
        92: r"\\",
    }
    return (
        '"'
        + "".join(escaped.get(c, chr(c) if 32 <= c < 127 else f"\\{c:03o}") for c in data)
        + '"'
    )


def public_file_patches(rows, diff):
    # Bind every raw Git section to the complete API file list; reject ambiguity.
    headers = {}
    for row in rows:
        name = row["filename"]
        previous = row.get("previous_filename") or name
        require(type(previous) is str, "FILE_PREVIOUS_NAME")
        a, b = "a/" + previous, "b/" + name
        for left in (a, git_quoted_path(a)):
            for right in (b, git_quoted_path(b)):
                header = "diff --git " + left + " " + right
                require(
                    header not in headers or headers[header] == name,
                    "DIFF_HEADER_AMBIGUOUS",
                )
                headers[header] = name
    sections, current = {}, None
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            require(line in headers, "DIFF_SECTION_UNKNOWN")
            current = headers[line]
            require(current not in sections, "DIFF_SECTION_DUPLICATE")
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
        else:
            require(not line, "DIFF_PREFIX_UNKNOWN")
    require(set(sections) == {r["filename"] for r in rows}, "DIFF_FILE_COVERAGE")
    return [
        {**r, "patch": "\n".join(sections[r["filename"]])}
        if r["filename"].startswith(("web/", "tests/e2e_ui/"))
        else dict(r)
        for r in rows
    ]


def pull_fence(row):
    image = []
    for branch in ("head", "base"):
        sha = row.get(branch, {}).get("sha")
        require(type(sha) is str and re.fullmatch(r"[0-9a-f]{40}", sha), "GATE_HEAD_BASE")
        image.append(sha)
    require(
        type(row.get("changed_files")) is int and 0 <= row["changed_files"] <= 200,
        "GATE_FILE_COUNT",
    )
    return (*image, row["changed_files"])


def read_body(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        require(
            stat.S_ISREG(before.st_mode) and 0 < before.st_size <= COMMENT_LIMIT,
            "COMMENT_FILE",
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
                response.status in ((200, 201) if method == "POST" else (200,)),
                "HTTP_REFUSED",
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
            require(
                content_type == ("text/plain" if text else "application/json"),
                "CONTENT_TYPE",
            )
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
                "GET",
                PREFIX + "/issues/" + number + "/comments?limit=50&page=" + str(page),
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
                    not next_page and (total is None or len(rows) == total),
                    "COMMENT_COVERAGE",
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
                        user.get("login") == self.binding["actor_login"],
                        "COMMENT_ACTOR_DRIFT",
                    )
                    url = row.get("html_url")
                    require(type(url) is str, "COMMENT_URL")
                    parsed = urlsplit(url)
                    require(
                        parsed.scheme == "https"
                        and parsed.netloc == "forge.ozba.me"
                        and parsed.path
                        in (
                            "/oz/omnigent/pulls/" + number,
                            "/oz/omnigent/issues/" + number,
                        )
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
        if action.startswith("gate-"):
            return self.gate_dispatch(action, number)
        if action == "reaction":
            self.transport.request(
                "POST",
                PREFIX + "/issues/comments/" + number + "/reactions",
                {"content": "eyes"},
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
                "POST",
                PREFIX + "/issues/" + number + "/comments",
                {"body": body_reader(path)},
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
                type(pull.get(branch)) is dict and type(pull[branch].get("ref")) is str,
                "PR_SHAPE",
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

    def gate_dispatch(self, action, number):
        if action == "gate-maintainers":
            value, _ = self.transport.request(
                "GET", PREFIX + "/contents/.github/MAINTAINER?ref=main"
            )
            require(
                type(value) is dict
                and value.get("type") == "file"
                and value.get("path") == ".github/MAINTAINER"
                and value.get("encoding") == "base64"
                and type(value.get("content")) is str,
                "MAINTAINERS_MAIN_FILE",
            )
            content = value["content"]
            require(len(content) <= 16384, "MAINTAINERS_BOUND")
            raw = base64.b64decode("".join(content.split()), validate=True)
            require(len(raw) <= 8192 and b"\0" not in raw, "MAINTAINERS_BOUND")
            raw.decode("utf-8")
            return base64.b64encode(raw).decode("ascii") + "\n"
        pull = self.pull(number)
        if action == "gate-title":
            require(type(pull.get("title")) is str, "GATE_TITLE")
            return pull["title"] + "\n"
        if action == "gate-author":
            login = pull.get("user", {}).get("login")
            require(
                type(login) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", login),
                "GATE_AUTHOR",
            )
            return login + "\n"
        if action == "gate-label":
            labels = pull.get("labels")
            require(
                type(labels) is list
                and all(type(r) is dict and type(r.get("name")) is str for r in labels),
                "GATE_LABELS",
            )
            return (
                "true" if any(r["name"] == "skip-e2e-ui-test" for r in labels) else "false"
            ) + "\n"
        before = pull_fence(pull)
        if action.startswith("gate-files-"):
            rows = paginated_gate_rows(
                self.transport, PREFIX + "/pulls/" + number + "/files", "filename"
            )
            require(len(rows) == before[2], "GATE_FILE_COVERAGE")
            for r in rows:
                require(
                    type(r.get("filename")) is str
                    and 0 < len(r["filename"].encode()) <= 4096
                    and type(r.get("status")) is str
                    and r["status"],
                    "GATE_FILE_SCHEMA",
                )
            if action == "gate-files-json":
                diff, _ = self.transport.request(
                    "GET", PREFIX + "/pulls/" + number + ".diff", text=True
                )
                rows = public_file_patches(rows, diff)
            require(before == pull_fence(self.pull(number)), "GATE_HEAD_BASE_DRIFT")
            if action == "gate-files-json":
                return json.dumps(rows, ensure_ascii=False) + "\n"

            def tsv(value):
                return (
                    value.replace("\\", "\\\\")
                    .replace("\t", "\\t")
                    .replace("\r", "\\r")
                    .replace("\n", "\\n")
                )

            return "".join(tsv(r["status"]) + "\t" + tsv(r["filename"]) + "\n" for r in rows)
        require(action == "gate-approvers", "GATE_UNSUPPORTED_SITE")
        rows = paginated_gate_rows(self.transport, PREFIX + "/pulls/" + number + "/reviews", "id")
        latest = {}
        for row in rows:
            state, login, ident = (
                row.get("state"),
                row.get("user", {}).get("login"),
                row["id"],
            )
            require(
                ident > 0
                and type(login) is str
                and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", login)
                and type(row.get("dismissed")) is bool,
                "GATE_REVIEW_SCHEMA",
            )
            require(
                state
                in {
                    "APPROVED",
                    "COMMENT",
                    "REQUEST_CHANGES",
                    "PENDING",
                    "REQUEST_REVIEW",
                },
                "GATE_REVIEW_STATE",
            )
            if state == "COMMENT" and not row["dismissed"]:
                continue
            # A dismissed approval cannot count; later comments leave decisive reviews intact.
            stamp = row.get("submitted_at")
            require(type(stamp) is str, "GATE_REVIEW_TIME")
            dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            require(dt.tzinfo is not None, "GATE_REVIEW_TIME")
            when = (dt.timestamp(), ident)
            if login not in latest or when > latest[login][0]:
                latest[login] = (when, "DISMISSED" if row["dismissed"] else state)
        require(before == pull_fence(self.pull(number)), "GATE_HEAD_BASE_DRIFT")
        return "".join(
            login + "\n" for login, (_, state) in sorted(latest.items()) if state == "APPROVED"
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
