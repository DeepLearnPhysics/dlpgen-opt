.PHONY: install test dry-run docker-build smoke

IMAGE ?= dlpgen-opt:0.1.0
PLATFORM ?= linux/amd64

install:
	python3 -m pip install -e '.[test]'

test:
	python3 -m pytest

dry-run:
	dlpgen-opt run configs/production.example.yaml --job 0 --dry-run

docker-build:
	docker build --platform $(PLATFORM) -t $(IMAGE) .

smoke:
	docker run --rm --platform $(PLATFORM) -v "$(CURDIR):/work" $(IMAGE) \
		run configs/production.smoke.yaml --job 0
