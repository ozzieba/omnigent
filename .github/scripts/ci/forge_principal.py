"""Observe the normal personal Forge Actions principal using GET requests only."""

import json
import os
import re
import urllib.error
import urllib.request

ORIGIN = "https://forge.ozba.me"
API = ORIGIN + "/api/v1"
REPOSITORY = "oz/omnigent"
MAX_RESPONSE_BYTES = 131072


class Refused(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Refused(code)


def context(environment):
    require(environment.get("GITHUB_SERVER_URL") == ORIGIN, "SERVER_SCOPE")
    require(environment.get("GITHUB_API_URL") == API, "API_SCOPE")
    require(environment.get("GITHUB_REPOSITORY") == REPOSITORY, "REPOSITORY_SCOPE")
    require(environment.get("GITHUB_EVENT_NAME") == "pull_request", "EVENT_SCOPE")
    source = environment.get("PROBE_SOURCE_SHA", "")
    run = environment.get("GITHUB_RUN_ID", "")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", source)), "SOURCE_SCHEMA")
    require(bool(re.fullmatch(r"[1-9][0-9]{0,19}", run)), "RUN_SCHEMA")
    return {"source_sha": source, "run_id": run}


def observe(binding, read):
    user = read("/user")
    require(isinstance(user, dict), "USER_SCHEMA")
    require(type(user.get("id")) is int and -(2**63) <= user["id"] < 2**63, "USER_ID_SCHEMA")
    login = user.get("login")
    require(
        isinstance(login, str) and bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", login)),
        "USER_LOGIN_SCHEMA",
    )
    repo = read("/repos/" + REPOSITORY)
    require(isinstance(repo, dict) and type(repo.get("id")) is int, "REPO_SCHEMA")
    require(repo["id"] == 11 and repo.get("full_name") == REPOSITORY, "REPO_BINDING")
    return {
        "outcome": "OBSERVED",
        **binding,
        "principal_id": user["id"],
        "principal_login": login,
        "user_type": user.get("type") if user.get("type") in ("User", "Bot") else None,
        "is_bot": user.get("is_bot") if type(user.get("is_bot")) is bool else None,
        "repo_id": repo["id"],
        "bot_admission": "UNQUALIFIED",
    }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def reader(token, opener):
    require(
        isinstance(token, str) and 0 < len(token) <= 4096 and not any(c.isspace() for c in token),
        "TOKEN_SCHEMA",
    )

    def read(path):
        require(path in ("/user", "/repos/" + REPOSITORY), "READ_SCOPE")
        request = urllib.request.Request(
            API + path,
            headers={"Authorization": "token " + token, "Accept": "application/json"},
            method="GET",
        )
        with opener.open(request, timeout=12) as response:
            require(response.status == 200, "HTTP_STATUS")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            require(len(data) <= MAX_RESPONSE_BYTES, "RESPONSE_BOUND")
            return json.loads(data)

    return read


def main():
    try:
        binding = context(os.environ)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        result = observe(binding, reader(os.environ.get("GH_TOKEN", ""), opener))
    except Refused as error:
        result = {"outcome": "REFUSED", "code": str(error)}
    except urllib.error.HTTPError as error:
        result = {"outcome": "HTTP_ERROR", "status": error.code}
        error.close()
    except (OSError, ValueError) as error:
        result = {"outcome": "ERROR", "error_class": type(error).__name__}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["outcome"] == "OBSERVED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
