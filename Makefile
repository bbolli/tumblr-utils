all: README README.inc

README.inc: README.md
	markdown <$^ | perl -pe's!(</?h)(\d)>!$$1.($$2+1).">"!ge' >$@

README.html: README.md
	-markdown <$^ | tidy -utf8 -asxml -i -n -q >$@

README: README.html
	w3m -dump $^ >$@

.PHONY: all
