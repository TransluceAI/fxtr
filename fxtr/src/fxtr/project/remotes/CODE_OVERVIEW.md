# Remotes — code overview

`fxtr/project/remotes/` holds remotes: places a project pushes jobs to and pulls jobs from, and
the commands that set them up (`fxtr push`, `fxtr pull`, `fxtr remote`, `fxtr hub`). Of the rest
of fxtr, only `project/cli.py` imports this package. The hooks it uses elsewhere are named under
"What it uses outside this directory" below.

```
remotes/
├── __init__.py      Remote (the push and pull flow, and the steps a kind implements), PushedJob,
│                    Download, RemoteError, JOB_VISIBILITIES, the registry of kinds
│                    (register_kind), remote_for, implied_remote, push_job and pull_job,
│                    pull_command
├── files.py         the edits `fxtr remote` makes to the two configuration files
│                    (add_remote, rename_remote, remove_remote, set_default_remote)
├── hub.py           the hub kind: jobs published to a fxtr hub through the HubClient interface,
│                    HttpHubClient (the hub's /v1 API, stdlib urllib, and create_project),
│                    tokens (hub_token, save_token, credentials_path, check_hub_url),
│                    HUB_SETTINGS
├── hub_login.py     browser-confirmed terminal sign-in and credential polling
├── oci.py           the oci kind: jobs as OCI artifacts through the RegistryClient interface, default_tag,
│                    job_annotations, OCI_SETTINGS
└── oras_client.py   OrasRegistryClient, the registry client of the oci extra; auth_files
```

## Remotes

**A push goes to a configured remote, as with git.** A project's remotes are tables in two
files: `[tool.fxtr.remotes.<name>]` of `pyproject.toml`, committed, so every checkout has them,
and `[remotes.<name>]` of `fxtr.local.toml`, this machine's own. Each file may set
`default_remote` (in `[tool.fxtr]`, or at the top of the local file); the local one wins. A name
in both files is a `ConfigError`. `config.py` merges them (`_remotes_config`) into
`Project.remotes`, a `RemotesConfig` whose `by_name` holds each remote as a `RemoteConfig` with
a `kind`, the file it came from (`RemoteConfig.file`, `local`, `where` for messages), and the
kind's other settings as written. `remote_for` builds the named remote, else the default, else
the only one (`implied_remote`, which `pull_command` also uses to leave the name out); a project
with no remote, or several and no default, is refused with the command that fixes it. A
`default_remote` that names no remote is refused only when a command needs the default
(`implied_remote`), so the `fxtr remote` commands that fix it still run. Nothing is looked up from the checkout: a hub remote names its project by id, so moving the project's
directory in its repository changes nothing.

**Remotes have a kind.** A kind declares the settings its table can hold and builds its remotes
(`register_kind`); `remote_for` reads the settings strictly when it builds one
(`config.read_settings`), so a project with a remote it cannot use still loads. The built-in kinds,
`hub` and `oci`, register themselves when their modules load, and `remote_for` loads them before
it looks a kind up, so importing the package to parse the command line loads no transport.

**`fxtr remote` edits the files** (`remotes/files.py`). `add_remote`,
`rename_remote`, `remove_remote`, and `set_default_remote` edit a file's text with `tomlkit`,
which keeps its comments and layout, then read both files again (`config.load_remotes`) and
put them back if the result does not read. A new remote goes to `pyproject.toml` unless
`--local`; the first one becomes the default in the same file; renaming or removing a remote
follows any `default_remote` that names it; a committed default may not name a local remote.
After an edit that can touch `pyproject.toml`, the commands run
`LocalEnvironment.refresh_environment`, because uv treats any edit to that file as a change to the
installed project. A failure there is a note, since the edit has succeeded.
The commands need no database. `remote add --create` also makes the hub project
(`HttpHubClient.create_project`) for the checkout's GitHub repository and project directory
(`Environment.repository_binding`, `github_repository`), so it works in a fresh clone before
`fxtr init`; anyone who can read the repository may. An SSH host name in the git remote's URL counts
as GitHub when the user's SSH configuration resolves it to `github.com` (`ssh -G`), which is how a
second GitHub account's key is usually set up. In a terminal the command first does what the hub
needs, in the browser: it connects the computer (`hub_login.browser_login`), and it waits for the
hub's GitHub App to be installed on the repository (`hub_login.create_project_after_install`). Its
link to GitHub carries `hub_login.INSTALL_STATE`, by which the hub's new-project page sends the
person back to the terminal. Without a terminal it stops with the hub's refusal.

**Push and pull are not client methods.** `push_job` and `pull_job` are functions over the
`ProjectClient` interface: a remote does its work with the client's methods (`export_job`,
`import_job`), so a pull refuses what an import refuses; with `update` it brings a job the
project has up to date, and with `replace` it replaces the project's copy of the job. A pull whose
commits the git remote does not give reports that and the job's page on the hub, and imports
nothing. They take the project and the environment from the client
(`ProjectClient.project`, `ProjectClient.environment`).

**One job per push or pull, the same way on every kind.** `Remote.push` and `Remote.pull` are
the flow; a kind implements the steps that differ. A push calls the kind's `check_push` (what the
push asks for that the kind cannot do, refused before anything is exported: a visibility on a
kind without one, an invalid OCI tag; the `hub` kind checks its credentials and role here),
exports the job to
a temporary file, reads it (`read_record`), refuses a record naming a commit the checkout lacks
(`Environment.missing_commits`, where git would fail with its own message), pushes the commits to
the remote's `git_remote` under fxtr's pin refs (`Environment.push_commits`), and calls the kind's
`upload`, which sends the file, sets the tags, and returns the `PushedJob`. A pull calls the kind's
`download`, which writes the file `ref` names and returns a `Download` (what was served, for
messages, and, when the remote says, the record and job it should be), reads the file under
`job_record.IMPORT_LIMITS` (the sizes the release hub accepts of a push), refusing a file that is
not a record this fxtr reads, or not the record the remote said, as a `RemoteError` (so the
command line reports it as a refusal), fetches the commits the repository lacks
(`Environment.fetch_commits`), and imports it. A push sets the tags it is given, and returns
`PushedJob.ref`, what the printed pull command names (the job's id on a hub, tags or not; the
first tag on an OCI registry), and `PushedJob.version`, what the remote calls that push (the
hub's record cid of the job after the sync; the artifact's digest on a registry, which names
that push for ever). What a tag is, and whether a push with none sets a default,
is the kind's (the `oci` kind moves tags as `docker push` does; the `hub` kind's tags are
tags that move only from where the push saw them); what else a pull names is the kind's too
(the `hub` kind takes `tag:<name>` and `owner/repo/<job id>`, below).
There is no listing: a push prints the command that pulls what it pushed (`pull_command`, by
`PushedJob.ref`: the job id on a hub, tags or not, the first tag on a registry) and, in
parentheses, what the remote calls that push (`PushedJob.version`: the record cid on a hub,
the digest on a registry). A push of a record the remote already holds reports it as
`PushedJob.unchanged` (the `hub` kind sends nothing then; the `oci` kind pushes every time),
and the command line prints "already on" instead of "pushed to". On a hub, it also prints the
job's page (`PushedJob.url`). A push reports its stages through the `fxtr` loggers at INFO, which
the command line shows on stderr and `-q` hides; `push_job` prints nothing of its own.

**A job's address.** `fxtr pull` also takes the address of a job's page on a hub (`hub.job_page_remote`), which names the hub and project, so it needs no configured remote. Only the token saved for that hub goes with it, never `FXTR_HUB_TOKEN`, since an address can come from anyone.

**The command line.** `fxtr push [<job>] [--remote] [--tag ...]` (with no job, it asks which of
the newest jobs to push) and
`fxtr pull <job-id | owner/repo/<job-id> | tag:<name> | tag | @digest> [--remote] [--update | --replace] [--adopt-cache
[--overwrite-cache-conflicts]]` live in `project/cli.py`, in its remotes section, and call
`remote_for` and `pull_command`; the Python API also provides `push_job` and `pull_job`. A pull with `--adopt-cache` then calls
`client.adopt_cache`, as `fxtr jobs import --adopt-cache` does. `fxtr hub login [URL]`
opens a browser to confirm a computer connection, polls at the interval and for the lifetime
the hub returns, then saves the independently revocable credential for the `hub` kind; without a
URL it uses `default_hub_url` (`DEFAULT_HUB_URL`, `https://hub.transluce.org`, or
`FXTR_HUB_URL`), as `remote add` does. `--no-browser` prints the confirmation link;
`--token-stdin` reads a provisioned credential from standard input. It does not open a project
client. `fxtr remote add|list|rename|remove|set-default` call `remotes/files.py`
(above) and open no client either. `push
--visibility` and `fxtr hub visibility JOB LEVEL` set who can read a job on a hub
(`Remote.push` and `Remote.set_visibility`, which only the `hub` kind implements; every other
kind refuses a visibility with a `RemoteError`).

## The `hub` kind

The hub client validates tag names with `check_tag` and checks credentials and contribution
access before exporting a job or pushing commits. A push reports the visibility from its sync
receipt; an unchanged push uses the job metadata it read before skipping the upload.

**Jobs are synced to a fxtr hub, commits go to a git remote** (`remotes/hub.py`, whose
docstring is the reference). A hub project holds each job once: the facts of its record merged
into the hub's tables, named by the hub's *record cid* (the root of the record the hub would
export of the job), with the attachments identity of the last push. With the record exported
and its commits pushed (`Remote.push`), the kind's `upload` refuses a job no process has taken
yet (it has no commit it ran at), reads the job's state from the hub (`HubClient.job_state`),
and, unless the hub holds this record with these attachments, uploads the file and publishes it
expecting the hub's record cid (`None` for a job the hub does not hold), then waits for the hub
to merge it (the upload becomes `synced`, and its answer carries the hub's record cid as the
sync committed it, which is `PushedJob.version`) or refuse it (`record_conflict`: someone pushed
the job in between, or resumed it on both sides, reported as "pull it first", and for the second
case what `--replace` and a new job do; a missing pin, a limit, a malformed record).
The upload's idempotency key is a hash of the file, the expected record cid, the attachments
identity, and any visibility asked for, so a retried push finds the upload it made and sends
only the bytes the hub lacks, in requests of at most `SEND_CHUNK_BYTES`; an upload an earlier
push left failed or expired is passed over for one keyed after it (at most
`MAX_PASSED_OVER_UPLOADS` times, then the hub is given up). A visibility asked for goes with the
upload, and the hub applies it to a job it did not hold; for a job it holds, the push sets it
once synced. Each tag the push names is then set on the hub, moved only from where the push
saw it (compare-and-swap); a tag not set, for whatever reason, is reported with the synced
job, which stands. A push returns the job's id as its `ref`. The kind's `download` resolves its
reference (`HubRemote._resolve`) to a job id: a UUID or `owner/repo/<uuid>` (or `repo/<uuid>`)
is parsed, not looked up, and `HubRemote.job_id` checks the repository, without case, against
the checkout's GitHub origin (`Environment.repository_binding`), never selecting the hub project
by it; anything else names a tag, and `tag:<name>` a tag whose name looks like a job
reference. `fxtr push` takes
the same job references, through `HubRemote.job_id`, when the remote is a hub. `download` then
gets the job's archive (`GET .../jobs/<id>/archive`, the record the hub exports of what it
holds; a 404 is "no readable job") and says which record it should be, from the
`X-Fxtr-Record-Cid` header of that same answer, so no earlier request can race another sync;
`Remote.pull` checks it.

**The hub client is an interface.** `HubRemote` reaches the hub through `HubClient`, which tests fake;
it carries the hub project's id. `HttpHubClient` speaks the hub's `/v1` HTTP API with the standard library's `urllib` in a worker
thread, so the kind needs no extra; it sends a bearer token from `FXTR_HUB_TOKEN`, else the token
`save_token` wrote for the hub's URL in `$XDG_CONFIG_HOME/fxtr/hub.toml` (mode 0600), never one in
`fxtr.local.toml`, and follows no redirect, so the token goes only to that URL. One rule,
`check_hub_url`, says where a token may go (HTTPS, or HTTP to a loopback host for a development
hub); `HttpHubClient` applies it when built with a token, `save_token` before it saves one, and
`default_hub_url`, `browser_login`, and `save_token` to the origins they take (a token is looked up
by a remote's `url`, which is an origin). The hub's
refusals are `HubRequestError`s carrying its status and error code; an answer of the wrong shape
(a field missing, an empty body where an object is due) is read through `_Answer` and is a
`RemoteError` naming the request, and an answer or a download over `MAX_ANSWER_BYTES` or
`job_record.MAX_RECORD_FILE_BYTES` is refused as it is read; a hub not reached is reported with
the system's reason ("Connection refused"), unwrapped from urllib's error. The settings are
`url`, `project` (the hub project's id), and `git_remote`, each a nonempty string.

**What goes beside the record.** The hub sends the project's built view bundle
(`fxtr views build`'s `dist/`, if any) as the upload's attachment before it publishes.
All remote kinds preserve execution history in the record file. A local bundle has no recorded
build commit, so the client sends null provenance. `_Attachments.identity` (the bundle's version)
names what was attached; the upload key covers it, and the hub keeps it on the job as an opaque
string, so
a push skips only when the hub holds the same record with the same attachments (the record cid
alone does not tell a rebuilt bundle apart). A checkout with no built bundle has no say on
attachments, since `dist/` is not committed: the job keeps the ones it has. The hub shows the job with the bundle the push
attached.

## The `oci` kind

**Jobs live in an OCI registry, commits in a git remote** (`remotes/oci.py`). Each push is one
artifact in the remote's `repository`: artifact type
`application/vnd.transluce.fxtr.job-record.v0.1`, the empty config, one layer (the record file,
`application/vnd.transluce.fxtr.job-record.car+gzip`), and annotations naming the job, its
workflow, root, state, and record (`job_annotations`), beside the remote's own `annotations`
setting (for example `org.opencontainers.image.source`; `check_annotations` refuses the names
fxtr writes). A push with no tags sets the default tag `<workflow>-<YYYYMMDDTHHMMSSZ>-<job id>`
(`default_tag`). The kind's `download` refuses a manifest of another artifact type, with other
than one layer, or whose layer is over `MAX_RECORD_FILE_BYTES`, before it downloads anything.
The registry client verifies the manifest's digest and hashes
the layer as it arrives, refusing a different digest or a layer over the byte bound. The
remote passes the manifest's record and job annotations to the shared pull check.

**The registry client is an interface.** `OciRemote` reaches the registry through `RegistryClient`
(`push_artifact`, `fetch_manifest`, `fetch_layer`), which tests fake. The real client,
`OrasRegistryClient` (`remotes/oras_client.py`, the `oci` extra), uses oras-py's
lower-level registry API and builds the manifest itself, since oras-py's own push cannot set an
artifact type; oras-py is synchronous, so its calls run in a worker thread. Credentials come from
the files that `docker login`, `podman login`, and `oras login` write (`auth_files`), or only
from the remote's `auth_file` when it names one; the `insecure` setting talks plain HTTP to a
local registry. `oci.build` imports the client only when it builds a remote, and asks for
`fxtr[oci]` when it cannot.

## What it uses outside this directory

- `project/config.py`: the `default_remote` and `remotes` settings of both files,
  `RemotesConfig`, `RemoteConfig`, `Project.remotes`, and `load_remotes`.
- `project/client.py`: `ProjectClient.project` and `ProjectClient.environment`.
- `project/environment.py`: `Environment.push_commits` and `Environment.fetch_commits`, which
  raise unless an environment implements them (`LocalEnvironment` does, over git),
  `Environment.missing_commits`, and `Environment.repository_binding`.
- `project/job_record.py`: `read_record`, `RecordFile` (`commits`, `commit_of`),
  `RecordFileError`, `IMPORT_LIMITS`, and `MAX_RECORD_FILE_BYTES`.
- `fxtr/pyproject.toml`: the `oci` extra, and the `tomlkit` dependency that
  `remotes/files.py` writes with.
- `project/cli.py`: the `push`, `pull`, `remote`, and `hub` commands.

## Tests

- `tests/test_project_remotes.py`: choosing a remote (refused with none) and
  reading its kind's settings, the pull command, and pushing and pulling through the in-memory `memory` kind of
  `tests/project_remotes_testing.py`, which implements only `upload` and `download` and so
  exercises the flow of `Remote` (with `update`, by ref, an unknown tag).
- `tests/test_project_remotes_oci.py`: the `oci` kind over a fake registry: default
  tags, invalid tags, push and pull between two projects (the artifact, its annotations, commits
  pushed and fetched), a record naming a commit the checkout lacks, tags moving, refusing what is
  not a pushed job or not a record, the kind's settings,
  building a remote with the oras client, credential files, and the message when the extra is
  missing.
- `tests/test_project_remotes_hub.py`: the `hub` kind over a fake hub that keeps the
  hub's rules: push and pull between two projects (commits pushed and fetched, by tag and by
  job id), a job that never ran, a record naming a commit the checkout lacks, an unchanged job
  pushed again, a resumed job expecting the hub's record, a record that changed meanwhile, a
  tag that moved or was refused, a retried push resuming its upload, a push after a refused
  one, an expired upload, a hub that fails every upload, a download that is not the job named
  or not a record, the pull limits, unknown names; `HttpHubClient` against
  local HTTP servers (routes, the token, bounded chunks from the hub's offsets, a complete upload
  not sent again, no redirect followed, the hub's errors and a hub not reached, answers of the
  wrong shape or over the bound); the settings (required ones nonempty); saved tokens and where a
  token may go (an origin); `fxtr hub login` (its flags, `--token-stdin`).
- `tests/test_project_remotes_registry.py`: the `oci` kind and `OrasRegistryClient`
  against a real registry, skipped unless `FXTR_TEST_OCI_REGISTRY` is set (its docstring says how
  to start one locally): the client's push and fetches, a job moving between two projects, a
  foreign artifact refused.
- `tests/test_project_remotes_files.py`: `fxtr remote` through the
  command line: what each command writes and keeps in each file, the default, names across both
  files, and refusals that leave the files as they were.
- `tests/test_project_config.py` reads the remotes of both files and merges them, and
  `tests/test_project_environment.py` pushes and fetches pin refs through a bare repository.
- `tests/test_project_cli.py`: `fxtr push|pull` through the `memory` kind (which
  reports a record it holds as unchanged, as the `hub` kind does).
