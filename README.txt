==============================
 GitHub: Migrate Trac Tickets
==============================

Forked from https://github.com/koansys/github-migrate-trac-tickets by Stuart Wakefield

Modifications from original
=================
* Add --json method to dump tickets as importable json files - see https://gist.github.com/7f75ced1fa7576412901.
* Exisitng upload functionality should still work, but has not been tested.
* Can specify component to migrate.
* Map from trac to github usernames (see --authors-file option).
* Map svn commit id's to git commits (see --checkout option). Warning: very slow.
* Map keywords, severity, priority, resolution and type to labels (can be edited in github later if needed).
* Add ticket reporter.
* Fix issue with dates and add create and update times for tickets. No timezone conversion.
* No longer add component to issue labels. Assuming one repo per componenet.

==============================
Origignal documentation
==============================

GitHub Issues API
=================

Offer a minimal API to work with GitHub's Issues via the v3 API.

It can GET issues, comments, labels, milestones, and can POST data to
create new ones via a simple dictionary.

Migrate Trac Tickets to GitHub Issues
=====================================

A sample program uses this to migrate Trac tickets into GitHub Issues.

It creates and merges "Milestones".

It uses Trac "Components" as GitHub "Labels".

It cannot migrate ticket ownership to GitHub Issue "Assignee" since we
have no way to map customer-specific Trac usernames into global GitHub
usernames.

Testing out migration
---------------------

The GitHub API has no way to DELETE an Issue, so you might want to
test out the migration first.

Create a temporary new GitHub repository, like 'yourorg/killme'. Then
push your Trac tickets into it, for example:

  ./trac-tickets-to-gh.py ~/oldproject-trac.db yourname yourpasswd yourorg/killme

Then verify labels and milestones migrated as expected. Finally,
destroy the test repository.

If you've (cough) already migrated tickets into an existing github with
code, you can ensure you've pulled a current copy of the code repo,
then blow it away and re-create, then migrate the tickets, and finally
push your local code repo back into the code repo.  You'll lose any
tickets that were created within GitHub that aren't in your Trac,
however.

