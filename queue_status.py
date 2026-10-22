"""Informational GitHub commit statuses, scoped to one target branch.

These statuses are UI feedback, not an authorization boundary or a required
candidate check. No token is persisted or included in diagnostics.
"""

import hashlib
import json
import os
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_REQUEST_ATTEMPTS = 3
RETRY_DELAYS = (2, 5)


class StatusError(RuntimeError):
    def __init__(self, message, transient=False):
        super().__init__(message)
        self.transient = transient


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, file, code, message, headers, new_url):
        return None


class CommitStatuses:
    def __init__(self, repository, token, run_id, api_url="https://api.github.com",
                 server_url="https://github.com"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository or "") or not token or not str(run_id).isdigit():
            raise StatusError("Missing GitHub repository, run ID or status credential")
        parsed = urlsplit(api_url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment or (
                parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost"))):
            raise StatusError("Unsafe GitHub status API URL")
        self.api_url = api_url.rstrip("/")
        self.repository = repository
        self.token = token
        self.target_url = server_url.rstrip("/") + "/" + repository + "/actions/runs/" + str(run_id)
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_environment(cls):
        env = os.environ
        if not any(env.get(key) for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_RUN_ID")):
            return None
        return cls(env.get("GITHUB_REPOSITORY"), env.get("GITHUB_TOKEN"), env.get("GITHUB_RUN_ID"),
                   env.get("GITHUB_API_URL", "https://api.github.com"),
                   env.get("GITHUB_SERVER_URL", "https://github.com"))

    @staticmethod
    def context(target):
        # GitHub contexts are case-insensitive whereas Git refs are not.
        suffix = hashlib.sha256(target.encode("utf-8")).hexdigest()[:12]
        return "council/" + target[:65] + "#" + suffix

    def request_once(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(self.api_url + path, data=data, method=method,
                          headers={"Accept": "application/vnd.github+json", "Authorization": "Bearer " + self.token,
                                   "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with self.opener.open(request, timeout=20) as response:
                if response.status == 204:
                    return None
                return json.load(response)
        except HTTPError as error:
            raise StatusError("GitHub status API returned HTTP " + str(error.code),
                              transient=error.code >= 500 or error.code == 429) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise StatusError("GitHub status API response is unavailable or invalid", transient=True) from None

    def request(self, method, path, payload=None):
        # Only reads are repeated here; a POST may have been applied before its
        # response was lost, so its callers read back before any repetition.
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            try:
                return self.request_once(method, path, payload)
            except StatusError as error:
                if method != "GET" or not error.transient or attempt == MAX_REQUEST_ATTEMPTS:
                    raise
                time.sleep(RETRY_DELAYS[attempt - 1])

    def latest(self, sha, context):
        page = 1
        while True:
            items = self.request("GET", "/repos/" + self.repository + "/commits/" + quote(sha, safe="")
                                 + "/statuses?per_page=100&page=" + str(page))
            if not isinstance(items, list):
                raise StatusError("Invalid GitHub status list")
            for item in items:
                if item.get("context", "").casefold() == context.casefold():
                    return item
            if len(items) < 100:
                return None
            page += 1

    def send(self, sha, target, state, description):
        if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha or "") or state not in ("pending", "success", "failure", "error"):
            raise StatusError("Invalid status SHA or state")
        body = {"state": state, "context": self.context(target), "description": description[:140],
                "target_url": self.target_url}

        def matches(item):
            return item is not None and all(item.get(key) == value for key, value in body.items())

        previous = self.latest(sha, body["context"])
        if matches(previous):
            return dict(body, sha=sha, id=previous.get("id"), deduplicated=True)
        for attempt in range(1, MAX_REQUEST_ATTEMPTS + 1):
            try:
                posted = self.request("POST", "/repos/" + self.repository + "/statuses/" + sha, body)
                break
            except StatusError as error:
                # A lost response may follow a successful POST. Read before
                # repeating so a retry does not create another identical status.
                observed = self.latest(sha, body["context"])
                if matches(observed):
                    return dict(body, sha=sha, id=observed.get("id"), deduplicated=True, post_attempts=attempt)
                if not error.transient or attempt == MAX_REQUEST_ATTEMPTS:
                    raise
                time.sleep(RETRY_DELAYS[attempt - 1])
        if not matches(posted):
            raise StatusError("GitHub did not confirm the requested status")
        return dict(body, sha=sha, id=posted.get("id"), deduplicated=False, post_attempts=attempt)
