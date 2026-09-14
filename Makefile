# Shortcuts for the common commands. Type "make" to see them.
IMAGE    ?= classroom-jupyterhub
TAG      ?= dev
PLATFORM ?= linux/amd64

.PHONY: help build build-server up down restart logs test shell export venv test-unit test-api check-config test-integration test-all console-logs demo

help:
	@echo "make build         build the image for THIS machine (fast, for local testing)"
	@echo "make up            start the Hub in the background   -> http://localhost:8000"
	@echo "make test          run the package smoke test inside the running container"
	@echo "make logs          follow the container logs"
	@echo "make shell         open a root shell inside the container"
	@echo "make down          stop the Hub (data is kept in volumes)"
	@echo "make build-server  build for the Linux x86_64 classroom server (TAG=1.1)"
	@echo "make export        save the image to $(IMAGE)-$(TAG).tar.gz for offline use"
	@echo "make test-unit         console unit + API tests on this Mac (make venv first)"
	@echo "make test-api          console tests inside the running container"
	@echo "make check-config      validate jupyterhub_config.py + custom templates in the container"
	@echo "make test-integration  end-to-end console check inside the container (login, add, remove)"
	@echo "make test-all          check-config + test-api + test-integration + smoke test"
	@echo "make console-logs      follow the teacher console log"
	@echo "make demo          Docker-free complete demo (console + sign-in) at http://127.0.0.1:8099"

build:
	docker build --build-arg VERSION=$(TAG) -t $(IMAGE):$(TAG) .

build-server:
	docker buildx build --platform $(PLATFORM) --build-arg VERSION=$(TAG) -t $(IMAGE):$(TAG) --load .

up:
	TAG=$(TAG) docker compose up -d

down:
	docker compose down

restart:
	docker compose restart

logs:
	docker compose logs -f

test:
	docker compose exec jupyterhub python /srv/smoke_test.py

shell:
	docker compose exec jupyterhub bash

export:
	bash -o pipefail -c 'docker save $(IMAGE):$(TAG) | gzip > $(IMAGE)-$(TAG).tar.gz.part'
	mv $(IMAGE)-$(TAG).tar.gz.part $(IMAGE)-$(TAG).tar.gz
	@ls -lh $(IMAGE)-$(TAG).tar.gz

venv:
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-console.txt httpx jinja2 psutil pillow nbformat

test-unit:
	.venv/bin/python -m pytest tests/console -q -p no:cacheprovider

test-api:
	docker compose exec -w /opt/classroom jupyterhub python -m pytest tests/console -q -p no:cacheprovider

check-config:
	docker compose exec -w /opt/classroom jupyterhub python tests/check_config.py

test-integration:
	docker compose exec -w /opt/classroom jupyterhub python tests/integration/console_check.py

test-all: check-config test-api test-integration test

console-logs:
	docker compose exec jupyterhub tail -f /srv/jupyterhub/logs/console.log

demo:
	@test -x .venv/bin/python || $(MAKE) venv
	.venv/bin/python -c "import jupyterhub" >/dev/null 2>&1 || .venv/bin/pip install -q 'jupyterhub>=5.3,<6'
	.venv/bin/python -m demo
