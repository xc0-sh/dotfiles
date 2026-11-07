.PHONY: check lint test

check: lint test

lint:
	git ls-files -co --exclude-standard '*.sh' | sort -u | xargs -r shellcheck
	shellcheck -s sh iso/preseed/00xc0-guard
	python3 -m json.tool iso/upstream.json >/dev/null
	python3 -m json.tool iso/ventoy.json >/dev/null

test:
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/iso -p 'test_*.py' -v
	PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests/bootstrap -p 'test_*.py' -v
