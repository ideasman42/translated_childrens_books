# Build story PDFs into ./build/ via _misc/rst_to_sphinx.py.
# List a book's directory in BOOKS; its targets and build rule are generated.
# `make` builds all, `make <book>` builds one. Per-book settings
# (my-pdf:font, my-pdf:font-size) live in each .rst file.

BOOKS := \
	der_grüne_esel \
	die_kleine_meerjungfrau \
	katzenweihnacht \
	tanzen_will_ich_tanzen

all: $(BOOKS)
$(BOOKS): %: build/%.pdf

build/%.pdf: %/translated_en_gb.rst _misc/rst_to_sphinx.py | build
	python3 _misc/rst_to_sphinx.py $< --output $@

build:
	mkdir -p $@

clean:
	rm -rf build

check_all: check_mypy check_ruff check_pylint

check_mypy:
	mypy --strict _misc/rst_to_sphinx.py

check_ruff:
	ruff check _misc/rst_to_sphinx.py

check_pylint:
	pylint --disable=R0913,R0917 _misc/rst_to_sphinx.py

.PHONY: all clean check_all check_mypy check_ruff check_pylint $(BOOKS)
