PROJECT=casadi
GITHUBCOMMITER=casadibot
GITHUBREPO=casadi/casadi

all: trac.db revision.map github-migrate-trac-tickets
	cd github-migrate-trac-tickets && python trac-tickets-to-gh.py --repo-names main --revmap-files ../revision.map --repo-types svn --authors-file ../authors2.txt ../trac.db $(GITHUBCOMMITER)  $(GITHUBREPO)

trac.sql:
	echo "Missing trac.sql"
	echo "================"
	echo "As a first step, obtain a dump of your project trac database."
	echo "You need admin rights over the project."
	echo "Navigate to the project home location https://sourceforge.net/projects/casadi/"
	echo "Select 'Project Admin'->'Features'"
	echo "In the row of 'Backups', click hosted Apps."
	echo "Content to backup: Database"
	echo "Content format: plain"
	echo "Select trac from the drop down"
	
authors.txt: 
	echo "Missing authors.txt"
	echo "==================="
	echo "Create a file that maps commit names to git identifiers"
	echo "Format:"
	echo "my_commitname = John Doe <John.Doe@gmail.com>"
	echo "Email adresses are ultimately used by github as identifiers"
		
trac_mod.sql: trac.sql
	./mysql2sqlite.sh < $< > $@
	
trac.db: trac_mod.sql
	sqlite3 $@ '.read $<'

gitsvn:
	mkdir gitsvn
	
gitsvn/log.txt: gitsvn authors.txt
	cd gitsvn
	svn2git https://$(PROJECT).svn.sourceforge.net/svnroot/$(PROJECT) --authors ../authors.txt --verbose > log.txt

revision.map: gitsvn/log.txt gitsvn
	perl -pe 'if (/r(\d+) = ([0-9a-f]+)/) { $$_="$$1 => $$2\n"; }' $< | grep "=>" | grep -v http | sort -n > $@
	
$(PROJECT).wiki:
	git clone git@github.com:$(GITHUBREPO).wiki.git
	
clean:
	rm trac_mod.sql trac.db revision.map
