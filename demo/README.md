# Complete demo

Run the Teacher console and branded sign-in without building the 10 GB classroom image.

```bash
make venv
make demo
```

Open <http://127.0.0.1:8099>.

| Who | How |
|---|---|
| Student | Sign-in page: `priya` / `coral-otter-12` (and the other names on the landing page) |
| Teacher | Console is already signed in as `teacher`. Password `change-me-teacher` if you use the sign-in page. |

The seeded class is **Lincoln High School**, Grade 10 · Python & Radio Lab. Every console page has data: students online, handouts, submissions, an archived student, Hub logs, branding.

This process uses the project's test doubles (`FakeHub`, `FakeSystem`). No Linux accounts are created. Opening a student workspace lists that student's home files; notebook kernels run only inside the Docker image (`make up`).

Regenerate the lesson notebooks after editing `handouts/build.py`:

```bash
.venv/bin/python demo/handouts/build.py
```
