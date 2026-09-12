# Classroom JupyterHub in Docker

One Docker image that a teacher runs on one Linux computer. Students open a
browser on the same network, log in with their own username, and get JupyterLab
with Python, AI/ML libraries, OpenCV and SDR tooling already installed.
Nothing is installed on student laptops. Works with no internet once the image
is on the server.

---

## 0. Words you will see

| Word | Meaning |
|---|---|
| **Image** | A frozen snapshot of a whole Linux system with software installed. A zip file of a ready computer. |
| **Container** | A running copy of an image. Stop it and it is gone; only **volumes** keep data. |
| **Volume** | A folder Docker keeps outside the container so files survive restarts. Student notebooks live here. |
| **Dockerfile** | The recipe that builds the image: "start from X, install Y, copy Z". |
| **JupyterHub** | The login server in front of Jupyter. Each student gets their own private JupyterLab. |
| **Docker Compose** | A small file (`compose.yaml`) so the teacher types one command instead of a long one. |

How a student reaches their notebook:

```
student browser  ->  http://<server-ip>:8000  ->  JupyterHub login
                                                   -> JupyterLab running as that student's Linux user
                                                   -> files in /home/<student>   (on a volume)
```

## 1. What is in this folder

| File | What it does |
|---|---|
| `Dockerfile` | Builds the image: base image + apt packages + conda packages + pip packages + config. |
| `requirements.txt` | The pip package list. **Edit this to add libraries.** Radio section is a placeholder. |
| `jupyterhub_config.py` | JupyterHub settings: login method, how servers start, idle timeout. |
| `entrypoint.sh` | Runs at container start: creates Linux accounts from `users.txt`, then starts the Hub. |
| `users.txt` | **The teacher's file.** One `username:password` per line. Mounted, never baked into the image. |
| `pam-jupyterhub` | Tiny PAM rule so the Hub can check passwords against those Linux accounts. |
| `sitecustomize.py` | Loaded by every Python process. Fixes a PyTorch/OpenBLAS clash on ARM builds and hides one noisy warning. |
| `compose.yaml` | Ports, volumes, environment. `docker compose up -d` reads this. |
| `tests/smoke_test.py` | Imports every promised package and does a tiny calculation with each. |
| `Makefile` | Shortcuts: `make build`, `make up`, `make test`, `make export`. |

## 2. One-time setup on the Mac (development machine)

The Mac already has the Docker CLI and Colima (the engine). The `compose` and
`buildx` plugins were added with Homebrew. Start the engine:

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

Open <http://localhost:8000>. Log in with any line from `users.txt`, for example
`student01` / `changeme01`. The admin is `teacher`.

Run the package check inside the running container:

```bash
make test
```

Or in a notebook cell: `%run /srv/smoke_test.py`.

Stop it (student files are kept in the volumes):

```bash
make down
```

## 4. Build for the Linux classroom server and export offline

The server is Linux x86_64, so build for that platform explicitly and give it a
real version tag:

```bash
make build-server TAG=1.0
```

This is slower than the local build because the Mac emulates an x86 CPU while
installing packages. The first run took about 17 minutes on this Mac; later runs
reuse the cache.

Save it to one file for the USB stick:

```bash
make export TAG=1.0
```

The file is about 2.2 GB (the image is stored compressed). Copy these to the
server: `classroom-jupyterhub-1.0.tar.gz`, `compose.yaml`, `users.txt`, and
this `README.md`.

## 5. On the Linux server (teacher's steps)

Docker Engine with the compose plugin must be installed once (needs internet
that one time). Then, in the folder with the copied files:

```bash
docker load -i classroom-jupyterhub-1.0.tar.gz
```

```bash
TAG=1.0 docker compose up -d
```

Find the server's IP address:

```bash
hostname -I
```

Students open `http://<that-ip>:8000` in any browser on the same network.

**First time on the server, run the package check once.** The x86 image cannot
be fully tested on the Mac (PyTorch and TensorFlow hang under CPU emulation), so
this is the real proof that every library works:

```bash
docker compose exec jupyterhub python /srv/smoke_test.py
```

It should end with `All 18 checks passed`.

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

## 6. Everyday tasks

**Add a student or reset a password.** Edit `users.txt`, then:

```bash
docker compose restart
```

Passwords are re-applied from the file every start. Existing files are kept.

**Hand out files to students.** As `teacher`, put files in the `shared` folder
in JupyterLab. Every student sees the same folder as `~/shared`, read-only.

**See who is logged in / stop a student's server.** Log in as `teacher`, open
*File > Hub Control Panel > Admin*.

**Back up all student work.**

```bash
docker run --rm -v classroom_homes:/home -v "$PWD":/backup ubuntu tar czf /backup/homes-backup.tar.gz -C / home
```

The volume is called `classroom_homes` because `compose.yaml` fixes the project
name to `classroom`. List all three with `docker volume ls`.

**Add a library.** Add it to `requirements.txt`, rebuild (`make build-server TAG=1.1`),
re-export, re-load on the server, then `TAG=1.1 docker compose up -d`.

**Use an RTL-SDR dongle plugged into the server.** Uncomment the `devices:`
block in `compose.yaml` and restart. Linux only.

## 7. Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| Browser says "connection refused" from another laptop | Server firewall blocks port 8000. On Ubuntu: `sudo ufw allow 8000`. Also confirm both machines are on the same network. |
| Login fails for everyone | `users.txt` not mounted or malformed. Run `docker compose logs` and look for `[entrypoint] WARNING`. |
| "Spawn failed" after login | Look at `docker compose logs`. Usually the home volume is full or permissions changed. |
| A package is missing | Add it to `requirements.txt` and rebuild (section 6). |
| Port 8000 already in use | Change the left number in `ports: - "8000:8000"` in `compose.yaml`, e.g. `"8080:8000"`. |
| `docker compose ps` says `(unhealthy)` | The Hub stopped answering on port 8000. Read `docker compose logs` for the error, then `docker compose restart`. |
| Pressing Enter on the login form does nothing | Click the *Sign in* button. Some browsers need the click. |
| Students lost their running notebooks after a restart | Expected: restarting the container stops every server. Files on disk are kept; students log in again. |
| On the Mac, `docker image ls` shows the x86 image as only ~2 GB | Display quirk: Docker only counts layers unpacked for the Mac's own CPU. The exported tarball and the image on the Linux server have the full size. |
| Server is slow with many students | Lower `IDLE_TIMEOUT_SECONDS`, set `SHUTDOWN_ON_LOGOUT: "true"`, or give the server more RAM. Each active student typically uses 0.5-2 GB. |

## 8. Known limits (deliberate, for simplicity)

- Passwords in `users.txt` are plain text. Fine for a classroom LAN; not for the internet.
- No per-student CPU/RAM limits. The idle culler is the main protection against runaway usage.
- No HTTPS. Traffic is plain HTTP on the local network.
- CPU-only PyTorch/TensorFlow. Swap to CUDA wheels in the Dockerfile if the server has an NVIDIA GPU.
- `pyrtlsdr` is pinned to 0.3.0. Newer versions need a patched librtlsdr that neither Ubuntu nor conda ship.
- The radio package list is a placeholder until the real requirements are known (see `requirements.txt`).
