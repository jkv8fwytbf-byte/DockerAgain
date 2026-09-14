# Classroom JupyterHub in Docker

One Docker image that a teacher runs on one Linux computer. Students open a
browser on the same network, sign in with their own username, and get JupyterLab
with Python, AI/ML libraries, OpenCV and SDR tooling already installed. The
teacher manages the class from a built-in **Teacher console**. Nothing is
installed on student laptops, and nothing needs the internet once the image is
on the server.

---

## 0. Words you will see

| Word | Meaning |
|---|---|
| **Image** | A frozen snapshot of a whole Linux system with software installed. A zip file of a ready computer. |
| **Container** | A running copy of an image. Stop it and it is gone; only **volumes** keep data. |
| **Volume** | A folder Docker keeps outside the container so files survive restarts. Student notebooks, handouts and the class roster live here. |
| **Dockerfile** | The recipe that builds the image: "start from X, install Y, copy Z". |
| **JupyterHub** | The sign-in server in front of Jupyter. Each student gets their own private JupyterLab. |
| **Teacher console** | A web page inside the Hub, for teachers only, to add students, print login cards, share handouts, collect work, make backups and read the logs. |
| **Docker Compose** | A small file (`compose.yaml`) so the teacher types one command instead of a long one. |

How a student reaches their notebook:

```
student browser  ->  http://<server-ip>:8000  ->  JupyterHub sign-in page
                                                   -> JupyterLab running as that student's Linux user
                                                   -> files in /home/<student>   (on a volume)
teacher browser  ->  http://<server-ip>:8000/services/console/   (teachers only)
```

## 1. What is in this folder

| File / folder | What it does |
|---|---|
| `Dockerfile` | Builds the image: base image + system packages + AI/ML packages + console + config. |
| `requirements.txt` | The Python packages for students. **Edit this to add libraries.** Radio section is a placeholder. |
| `requirements-console.txt` | Packages the teacher console needs (kept separate so the big AI/ML layer never rebuilds for console work). |
| `jupyterhub_config.py` | JupyterHub settings: sign-in method, how servers start, idle timeout, the console service, branding hooks. |
| `entrypoint.sh` | Runs at container start: creates Linux accounts from the roster, seeds branding, starts the console and the Hub. |
| `users.txt` | **Seed file for the first start only.** One `username:password` per line. After that, manage students in the console. |
| `console/` | The Teacher console (Python + HTML/JS). `console/accounts.py` is also what creates accounts at start-up. |
| `hub-templates/` | The branded sign-in, error and waiting pages. |
| `branding/` | Default logo and branding values (copied to the volume on first start; the console edits the copies). |
| `skel/` | The welcome kit every new student gets: `Welcome.ipynb`, `submit/`, `notebooks/`. |
| `lab/overrides.json` | JupyterLab defaults for the class (no news popups, autosave, line numbers...). |
| `pam-jupyterhub` | Tiny PAM rule so the Hub can check passwords against the Linux accounts. |
| `sitecustomize.py` | Loaded by every Python process. Fixes a PyTorch/OpenBLAS clash on ARM builds and hides one noisy warning. |
| `compose.yaml` | Ports, volumes, environment. `docker compose up -d` reads this. |
| `tests/` | Smoke test, config check, console tests, end-to-end check. |
| `Makefile` | Shortcuts: `make build`, `make up`, `make test`, `make export`, ... (`make` lists them). |

## 2. One-time setup on the Mac (development machine)

The Mac has the Docker CLI and Colima (the engine). Start the engine:

```bash
colima start --cpu 4 --memory 8 --disk 60
```

Check everything answers:

```bash
docker info --format '{{.ServerVersion}}' && docker compose version && docker buildx version
```

## 3. Build and run locally

Build for this Mac (Apple Silicon, so this is a native ARM build and fast):

```bash
make build
```

First build downloads a few GB and takes 10-25 minutes. Later builds reuse the cache
and finish in seconds unless `requirements.txt` changed. The finished image is about
10 GB; TensorFlow is the single largest piece, so delete its two lines from
`requirements.txt` if the class will not use it.

Start it:

```bash
make up
```

Open <http://localhost:8000>. Sign in with any line from `users.txt`, for example
`student01` / `changeme01`. The teacher account is `teacher`.

Run the package check inside the running container:

```bash
make test
```

Stop it (student files are kept in the volumes):

```bash
make down
```

## 4. The Teacher console

Sign in as the teacher and click **Teacher console** in the top bar, or open
`http://<server-ip>:8000/services/console/`. Students never see this link and get
a "Teachers only" page if they try the address.

| Page | What you can do |
|---|---|
| **Dashboard** | Who is online, CPU/memory/disk of the server, stop or start a student's JupyterLab, stop all servers at the end of class. Updates every 5 seconds. |
| **Students** | Add a student (username and password are suggested for you), bulk add by pasting a class list, reset a password, remove a student (their files are archived, not deleted), show passwords, print login cards with a QR code. |
| **Handouts & Submissions** | Drop files into the shared folder every student sees as `~/shared` (read-only). Download each student's `~/submit` folder, or all of them as one zip. |
| **Backups** | One-click backup of every home folder, the handouts and the roster into a `.tar.gz` you can download; the archives of removed students live here too. |
| **Logs** | The Hub's and the console's own log, live, with a filter. Useful when something goes wrong. |
| **Settings** | School and class name, accent colour, an announcement for the sign-in page, the class address printed on login cards, the logo. Changes apply immediately, no restart. |

The console runs inside the same container as the Hub, listens only on the
container's own loopback address, and is reachable only through the Hub for
signed-in teachers. Teacher accounts are the names in `JUPYTERHUB_ADMIN_USERS`
in `compose.yaml` (default `teacher`).

## 5. Managing students: the console vs `users.txt`

- On the **first start** the container reads `users.txt` and creates the class
  roster from it (`roster.json` on the `hubstate` volume). If the teacher account
  is missing from `users.txt`, one is created with a random password that is
  printed once in `docker compose logs`.
- **After that, the console is the source of truth.** Editing `users.txt` changes
  nothing; the start-up log tells you so. To bring in a new list, use
  *Students > Bulk add > Import users.txt* (it adds missing students and can reset
  passwords; it never removes anyone).
- Passwords are stored in plain text in `roster.json` (root-only file) so login
  cards can be printed at any time. That is fine for a classroom LAN; it is not a
  design for the public internet.
- Removing a student stops their server, deletes the account and moves their
  home folder into an archive (`Backups > Archived students`). Nothing is lost
  until you delete the archive.
- Every container start re-creates the Linux accounts from the roster and keeps
  each student's files, because home folders live on the volume.

## 6. Branding

Settings page: school name, class name, accent colour (six choices), an
announcement shown on the sign-in page, the class address (used on login cards
and in the QR code; defaults to the address your browser used), and the logo
(PNG or JPEG up to 2 MB; SVG is not supported by the sign-in page). Everything is
stored under `/srv/jupyterhub/branding/` on the `hubstate` volume and takes
effect on the next page load.

## 7. Build for the Linux classroom server and export offline

The server is Linux x86_64, so build for that platform explicitly and give it a
real version tag:

```bash
make build-server TAG=1.1
```

This is slower than the local build because the Mac emulates an x86 CPU while
installing packages. The first run took about 17 minutes on this Mac; later runs
reuse the cache.

Save it to one file for the USB stick:

```bash
make export TAG=1.1
```

The verified 1.1 export is **2.32 GB (2.16 GiB; 2,316,253,677 bytes)**
(the image is stored compressed). Copy these to the
server: `classroom-jupyterhub-1.1.tar.gz`, `compose.yaml`, `users.txt`, and
this `README.md`.

For this export, the SHA-256 checksum is
`223ac273808ea5249c6ddda3257fd38b0db4108b781fe70be9b033d2ce213433`.
After copying, run `sha256sum classroom-jupyterhub-1.1.tar.gz` on Linux
(`shasum -a 256 classroom-jupyterhub-1.1.tar.gz` on the Mac) and compare the result.
A fresh rebuild may produce a different size and checksum.

## 8. On the Linux server (teacher's steps)

Docker Engine with the compose plugin must be installed once (needs internet
that one time). Then, in the folder with the copied files:

```bash
docker load -i classroom-jupyterhub-1.1.tar.gz
```

```bash
TAG=1.1 docker compose up -d
```

Find the server's IP address:

```bash
hostname -I
```

Students open `http://<that-ip>:8000` in any browser on the same network. The
teacher opens `http://<that-ip>:8000/services/console/`.

**First time on the server, run the checks once.** The x86 image cannot be fully
tested on the Mac (PyTorch and TensorFlow hang under CPU emulation), so this is
the real proof that everything works:

```bash
docker compose exec jupyterhub python /srv/smoke_test.py
```

```bash
docker compose exec -w /opt/classroom jupyterhub python tests/integration/console_check.py
```

Both should end with "passed".

Stop / start / see logs:

```bash
docker compose stop
```
```bash
docker compose start
```
```bash
docker compose logs -f
```

## 9. Upgrading from image 1.0

1. Load the new tarball and start it with the new tag:

   ```bash
   docker load -i classroom-jupyterhub-1.1.tar.gz && TAG=1.1 docker compose up -d
   ```

2. On that first start the container reads your existing `users.txt`, creates
   `roster.json` from it, re-creates the same accounts with the same numeric IDs
   (student files are untouched), adds the welcome kit to existing homes exactly
   once, and writes the default branding. Nothing else to do.
3. Rolling back (`TAG=1.0 docker compose up -d`) works: the old image reads
   `users.txt` again. Students or passwords changed in the console since the
   upgrade are not in `users.txt`, so add them there first if you roll back.

## 10. Everyday tasks

**Add a student or reset a password.** Console > Students. Takes effect at once.

**Hand out files.** Console > Handouts, or as `teacher` in JupyterLab put files in
the `shared` folder. Every student sees them as `~/shared`, read-only.

**Collect work.** Students save into their `submit` folder (the Welcome notebook
explains it). Console > Submissions > Download.

**End of class.** Dashboard > Stop all servers frees the memory. Files are safe.

**Back up.** Console > Backups > Create backup now, then download the file to a
USB stick. The old command-line recipe still works for the volumes:

```bash
docker run --rm -v classroom_homes:/home -v "$PWD":/backup ubuntu tar czf /backup/homes-backup.tar.gz -C / home
```

**Restore a backup.** Manual: extract the `.tar.gz` (it contains `home/`,
`shared/`, `hubstate/roster.json` and a `manifest.json` with instructions) and
copy the folders back into the volumes with the container stopped.

**Add a library.** Add it to `requirements.txt`, rebuild (`make build-server TAG=1.2`),
re-export, re-load on the server, then `TAG=1.2 docker compose up -d`.

**Use an RTL-SDR dongle plugged into the server.** Uncomment the `devices:`
block in `compose.yaml` and restart. Linux only.

**Turn the console off** (emergency): set `CONSOLE_ENABLED: "0"` in
`compose.yaml` and `docker compose up -d`. Students are unaffected; accounts still
come from the roster.

## 11. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Browser says "connection refused" from another laptop | Server firewall blocks port 8000. On Ubuntu: `sudo ufw allow 8000`. Also confirm both machines are on the same network. |
| Sign-in fails for everyone | Read `docker compose logs` for `[accounts]` warnings. On a fresh server the teacher password is printed there once. |
| "Spawn failed" after sign-in | Look at Console > Logs. Usually the home volume is full or permissions changed. |
| Console page says "Teachers only" | That account is not in `JUPYTERHUB_ADMIN_USERS`. |
| No "Teacher console" link in the top bar | Same cause: you are not signed in as a teacher account. |
| Log shows `Cannot connect to managed service console` | The console crashed; see `/srv/jupyterhub/logs/console.log` (Console > Logs if it recovers, else `docker compose exec jupyterhub tail -50 /srv/jupyterhub/logs/console.log`). Set `CONSOLE_ENABLED: "0"` to keep teaching meanwhile. |
| I edited `users.txt` and nothing changed | Expected after the first start. Use Console > Students > Import users.txt. |
| A package is missing | Add it to `requirements.txt` and rebuild (section 10). |
| Port 8000 already in use | Change the left number in `ports: - "8000:8000"` in `compose.yaml`, e.g. `"8080:8000"`. |
| `docker compose ps` says `(unhealthy)` | The Hub stopped answering on port 8000. Read `docker compose logs` for the error, then `docker compose restart`. |
| Students lost their running notebooks after a restart | Expected: restarting the container stops every server. Files on disk are kept; students sign in again. |
| On the Mac, `docker image ls` shows the x86 image as only ~2 GB | Display quirk: Docker only counts layers unpacked for the Mac's own CPU. The exported tarball and the image on the Linux server have the full size. |
| Server is slow with many students | Lower `IDLE_TIMEOUT_SECONDS`, set `SHUTDOWN_ON_LOGOUT: "true"`, use Dashboard > Stop all, or give the server more RAM. Each active student typically uses 0.5-2 GB. |

## 12. Known limits (deliberate, for simplicity)

- Passwords are plain text in `roster.json`, visible in the console and on printed cards. Fine for a classroom LAN; not for the internet.
- The console runs as root inside the container (it creates Linux accounts). It is reachable only through the Hub, only for teacher accounts.
- No HTTPS. Traffic is plain HTTP on the local network, so the console cookie is not marked secure.
- No per-student CPU/RAM limits. The idle culler and "Stop all servers" are the main protections.
- Backups are unencrypted and include the roster with passwords. Keep them safe.
- CPU-only PyTorch/TensorFlow. Swap to CUDA wheels in the Dockerfile if the server has an NVIDIA GPU.
- `pyrtlsdr` is pinned to 0.3.0. Newer versions need a patched librtlsdr that neither Ubuntu nor conda ship.
- The radio package list is a placeholder until the real requirements are known (see `requirements.txt`).

## 13. For developers: tests

```bash
make venv          # once: a local Python env with the console's dependencies
```
```bash
make test-unit     # console unit + API tests on this Mac, no Docker needed
```
```bash
make check-config  # validate jupyterhub_config.py and the templates inside the container
```
```bash
make test-api      # the same test suite inside the container
```
```bash
make test-integration   # sign in, add, verify and remove a throw-away student, end to end
```
```bash
make test-all      # all of the above plus the package smoke test
```

Release 1.1 was checked on 13 September 2026: **471 console tests passed** on
macOS, native ARM Linux and the emulated x86 release image (one test in each
environment only applies to the other environment and is skipped). The native
container also passed the configuration, login/account integration and all 18
library checks. Every console page and the branded sign-in page were checked in
light and dark mode, including phone and tablet layouts. Student Python isolation,
shared-file permissions and existing home ownership were verified in the container.
Stopping and recreating the container preserved all 21 account IDs, the roster,
branding and welcome-kit markers.
The x86 library checks in section 8 still need to run on the classroom server.

## 14. Complete demo (no Docker)

A seeded Lincoln High School class — branded sign-in, every Teacher console page, login cards, and sample lesson notebooks — without building the 10 GB image. It uses the same FakeHub / FakeSystem doubles as `make test-unit`, so no Linux accounts are created on this computer.

```bash
make venv
make demo
```

Open <http://127.0.0.1:8099>. Sign in as `priya` / `coral-otter-12`, or open the teacher console (already signed in as `teacher`). Handouts live in `demo/handouts/` and are copied into every student's `shared/` folder. Full JupyterLab kernels still need the classroom image (`make up`).
