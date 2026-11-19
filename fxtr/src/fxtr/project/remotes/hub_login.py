"""Steps of setting up a hub that happen in the user's browser: connecting a terminal by
confirming a short code, and installing the hub's GitHub App on a repository."""

import platform
import time
import urllib.parse
import webbrowser

import anyio

from fxtr.project.remotes import RemoteError
from fxtr.project.remotes.hub import HttpHubClient, HubRequestError, check_hub_url

INSTALL_WAIT_SECONDS = 15 * 60
"""How long a command waits for an installation: as long as a browser login request lasts."""
INSTALL_POLL_SECONDS = 5
INSTALL_STATE = "fxtr-cli"
"""The ``state`` on this command's link to GitHub's installation page. When GitHub gives it back,
the hub's new-project page sends the person back to the terminal, since this command makes the
project and a project made on that page would be a second one."""


async def _open_link(target: str) -> None:
    try:
        await anyio.to_thread.run_sync(webbrowser.open, target)
    except webbrowser.Error:
        print("Could not open a browser automatically. Open the link above.", flush=True)


async def create_project_after_install(
    client: HttpHubClient,
    name: str,
    repository: str,
    project_path: str,
    install_url: str,
    *,
    open_browser: bool = True,
) -> str:
    """Send the user to install the hub's GitHub App on a repository, and make the hub project
    when the hub can see the repository. This function returns the project's id.

    GitHub has no call that installs an App for the first time, so the user does this on
    GitHub's page. Nothing tells this computer when they finish, so this function asks again.

    Args:
        install_url: The installation page, from the hub's refusal to make the project.

    Raises:
        RemoteError: If the hub refuses for another reason, or the wait ends.
    """
    parts = urllib.parse.urlsplit(install_url)
    query = urllib.parse.parse_qsl(parts.query) + [("state", INSTALL_STATE)]
    link = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    print(
        f"The hub reads {repository} through its GitHub App, which is not installed on it.\n"
        f"Open {link}\n"
        f"Choose the account that owns {repository}, then the repository. Waiting; press Ctrl-C"
        " to stop.",
        flush=True,
    )
    if open_browser:
        await _open_link(link)
    deadline = time.monotonic() + INSTALL_WAIT_SECONDS
    said_so = False
    while time.monotonic() < deadline:
        await anyio.sleep(INSTALL_POLL_SECONDS)
        try:
            project_id = await client.create_project(name, repository, project_path)
        except RemoteError as exc:
            if isinstance(exc, HubRequestError):
                if "install_url" in exc.details:
                    continue
                if exc.status < 500 and exc.status != 429:
                    raise
            if not said_so:
                print(f"Still waiting; the hub did not answer just now: {exc}", flush=True)
                said_so = True
        else:
            print(f"The GitHub App is installed on {repository}.", flush=True)
            return project_id
    raise RemoteError(
        f"The hub still cannot see {repository}. Install its GitHub App on the repository:"
        f" {install_url}, then run this command again."
    )


async def browser_login(url: str, *, open_browser: bool = True) -> str:
    """Connect this computer to the hub at ``url`` (an origin that passes
    :func:`~fxtr.project.remotes.hub.check_hub_url`) and return the credential the
    hub issues once the user confirms the code in the browser.

    Raises:
        RemoteError: If ``url`` is not such an origin, the hub has no browser login, or the
            request expires or fails.
    """
    url = check_hub_url(url, origin_only=True)
    client = HttpHubClient(url, "", None, remote="hub", timeout=15)
    try:
        request = await client.begin_login(platform.node() or "fxtr CLI")
    except HubRequestError as exc:
        if exc.status == 404:
            raise RemoteError(
                "This hub does not support browser login. "
                "Use --token-stdin with a credential from its administrator."
            ) from exc
        raise
    device = request.get("device_code")
    code = request.get("user_code")
    verification = request.get("verification_uri")
    lifetime = request.get("expires_in")
    interval = request.get("interval")
    if (
        not isinstance(device, str)
        or not isinstance(code, str)
        or not isinstance(verification, str)
        or type(lifetime) is not int
        or lifetime <= 0
        or type(interval) is not int
        or interval <= 0
    ):
        raise RemoteError("The hub returned an invalid connection request")
    verification = check_hub_url(verification)
    origin = urllib.parse.urlsplit(url)
    target_parts = urllib.parse.urlsplit(verification)
    try:
        expected_origin = (
            origin.scheme,
            origin.hostname,
            origin.port or (443 if origin.scheme == "https" else 80),
        )
        actual_origin = (
            target_parts.scheme,
            target_parts.hostname,
            target_parts.port or (443 if target_parts.scheme == "https" else 80),
        )
    except ValueError as exc:
        raise RemoteError("The hub returned an invalid connection page URL") from exc
    if actual_origin != expected_origin:
        raise RemoteError(
            "The hub returned a connection page on another origin: "
            f"{target_parts.scheme}://{target_parts.netloc}; expected {url}. "
            "Run fxtr hub login with the hub's public URL."
        )
    parameters = urllib.parse.parse_qsl(target_parts.query) + [("code", code)]
    target = urllib.parse.urlunsplit(
        target_parts._replace(query=urllib.parse.urlencode(parameters))
    )
    print(
        f"Open {target}\nConfirm the code {code} matches your browser, then choose Connect.",
        flush=True,
    )
    if open_browser:
        await _open_link(target)
    deadline = time.monotonic() + lifetime
    said_so = False
    while time.monotonic() < deadline:
        try:
            result = await client.poll_login(device)
        except RemoteError as exc:
            # The person may be approving this right now, and a failed check loses nothing: the
            # request lasts until its deadline. Only the hub's own answer ends it early.
            if isinstance(exc, HubRequestError) and exc.status < 500 and exc.status != 429:
                raise
            if not said_so:
                print(f"Still waiting; the hub did not answer just now: {exc}", flush=True)
                said_so = True
            await anyio.sleep(interval)
            continue
        status = result.get("status")
        if status == "connected":
            token = result.get("token")
            if not isinstance(token, str) or not token.startswith("hub_cli_"):
                raise RemoteError("The hub returned an invalid computer credential")
            return token
        if status not in ("pending", "slow_down"):
            raise RemoteError("The hub returned an unexpected connection status")
        if status == "slow_down":
            next_interval = result.get("interval")
            if type(next_interval) is not int or next_interval <= 0:
                raise RemoteError("The hub returned an invalid polling interval")
            interval = next_interval
        await anyio.sleep(interval)
    raise RemoteError("The connection request expired. Run fxtr hub login again.")
