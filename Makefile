# Shortcuts for the common commands. Type "make" to see them.
IMAGE    ?= classroom-jupyterhub
TAG      ?= dev
PLATFORM ?= linux/amd64

.PHONY: help build build-server up down restart logs test shell export

help:
	@echo "make build         build the image for THIS machine (fast, for local testing)"
	@echo "make up            start the Hub in the background   -> http://localhost:8000"
	@echo "make test          run the package smoke test inside the running container"
	@echo "make logs          follow the container logs"
	@echo "make shell         open a root shell inside the container"
	@echo "make down          stop the Hub (data is kept in volumes)"
	@echo "make build-server  build for the Linux x86_64 classroom server (TAG=1.0)"
	@echo "make export        save the image to $(IMAGE)-$(TAG).tar.gz for offline use"

build:
	docker build -t $(IMAGE):$(TAG) .

build-server:
	docker buildx build --platform $(PLATFORM) -t $(IMAGE):$(TAG) --load .

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
	docker save $(IMAGE):$(TAG) | gzip > $(IMAGE)-$(TAG).tar.gz
	@ls -lh $(IMAGE)-$(TAG).tar.gz
