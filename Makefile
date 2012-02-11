all: README.html

%.html: %.md
	markdown $^ >$@
