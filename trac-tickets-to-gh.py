#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Migrate trac tickets from DB into GitHub using v3 API.
# Transform milestones to milestones, components to labels.
# The code merges milestones and labels does NOT attempt to prevent
# duplicating tickets so you'll get multiples if you run repeatedly.
# See API docs: http://developer.github.com/v3/issues/

# TODO:
# - it's not getting ticket *changes* from 'comments', like milestone changed.
# - should I be migrating Trac 'keywords' to Issue 'labels'?
# - list Trac users, get GitHub collaborators, define a mapping for issue assignee.
# - the Trac style ticket refs like 'see #37' will ref wrong GitHub issue since numbers change

import datetime
# TODO: conditionalize and use 'json'
import logging
from optparse import OptionParser
import sqlite3
import re
from itertools import chain
import subprocess

from github import GitHub

class Trac(object):
    # We don't have a way to close (potentially nested) cursors

    def __init__(self, trac_db_path):
        self.trac_db_path = trac_db_path
        try:
            self.conn = sqlite3.connect(self.trac_db_path)
        except sqlite3.OperationalError, e:
            raise RuntimeError("Could not open trac db=%s e=%s" % (
                    self.trac_db_path, e))

    def sql(self, sql_query):
        """Create a new connection, send the SQL query, return response.
        We need unique cursors so queries in context of others work.
        """
        cursor = self.conn.cursor()
        cursor.execute(sql_query)
        return cursor

    def close(self):
        self.conn.close()

class AuthorMapping(object):
    """Take provided file and return author mapping object"""
    def __init__(self, map_file):
        self.mapping = {}
	if map_file:
            with open(map_file, 'r') as file_:
                for line in file_.readlines():
                    if not line.strip():
                        continue
	            match = re.compile(r'^([\w\s\@&\.\d]*) = (\w*) <(.*)>$').search(line)
	            if not match:
	                raise ValueError, 'Author line not in correct format: "%s"' % line
	            svn_user, github_user, email = match.groups()
	            self.mapping[svn_user.strip()] = {"login" : github_user.strip()}
	        # Ignore email for now, seems username is eough

    def __call__(self, username):
        if not self.mapping:
		return {"login" : username}
	# just take 1st user if given a list
        username = username.split(',')[0].strip()
	#if not username in self.mapping:
        #    print "%s = DMWMBot <USER@DOMAIN>" % username
	#    return {"login" : username}
        # Throw if author not in mapping
	return self.mapping[username]

def svn_to_git_commit_id(checkout, svn_id):
    """Get git commit id for given svn id"""
    if not checkout:
        return svn_id
    p = subprocess.Popen(['git', '--git-dir=%s/.git' % checkout,
	                 'log', '--grep=^git-svn-id:.*@%s' % svn_id, '--format=format:%H', '-n1' ],
			 stdout = subprocess.PIPE, stderr = subprocess.PIPE)
    if not p.wait():
        out = p.stdout.read().strip()
	if len(out) == 40:
	    return out
    # couldn't find commit
    return svn_id

# Warning: optparse is deprecated in python-2.7 in favor of argparse
usage = """
  %prog [options] trac_db_path github_username github_password github_repo

  The path might be something like "/tmp/trac.db"
  The github_repo combines user or organization and specific repo like "myorg/myapp"
"""
parser = OptionParser(usage=usage)
parser.add_option('-q', '--quiet', action="store_true", default=False,
                  help='Decrease logging of activity')
parser.add_option('-c', '--component', default=None, help='Component to migrate, default all')
parser.add_option('-j', '--json', action="store_true", default=False, help='Output to json files for github import, default to direct upload')
parser.add_option('--authors-file', default=None, help='Author mapping file, if not specified take usernames from trac as given')
parser.add_option('--checkout', default=None, help='git-svn checkout, used to map svn commits to git commits in tickets. This is slow.')

(options, args) = parser.parse_args()
try:
    [trac_db_path, github_username, github_password, github_repo] = args
except ValueError:
    parser.error('Wrong number of arguments')
if not '/' in github_repo:
    parser.error('Repo must be specified like "organization/project"')

if options.quiet:
    logging.basicConfig(level=logging.INFO)
else:
    logging.basicConfig(level=logging.DEBUG)

trac = Trac(trac_db_path)
if options.json:
    from github_json import GitHubJson
    github = GitHubJson(github_repo)
else:
    github = GitHub(github_username, github_password, github_repo)

# default to no mapping
author_mapping = AuthorMapping(options.authors_file)

epoch_to_iso = lambda x: datetime.datetime.fromtimestamp(x).isoformat()

svn_to_git_mapper = lambda match: svn_to_git_commit_id(options.checkout, match.group(1))

# Show the Trac usernames assigned to tickets as an FYI

#logging.info("Getting Trac ticket owners (will NOT be mapped to GitHub username)...")
#for (username,) in trac.sql('SELECT DISTINCT owner FROM ticket'):
#    if username:
#        username = username.split(',')[0].strip() # username returned is tuple like: ('phred',)
#        logging.debug("Trac ticket owner: %s" % username)

# Get GitHub labels; we'll merge Trac components into them

#logging.info("Getting existing GitHub labels...")
#labels = {}
#for label in github.labels():
#    labels[label['name']] = label['url'] # ignoring 'color'
#    logging.debug("label name=%s" % label['name'])

# Get any existing GitHub milestones so we can merge Trac into them.
# We need to reference them by numeric ID in tickets.
# API returns only 'open' issues by default, have to ask for closed like:
# curl -u 'USER:PASS' https://api.github.com/repos/USERNAME/REPONAME/milestones?state=closed

# Assume no milestones exist in github
#logging.info("Getting existing GitHub milestones...")
milestone_id = {}
#for m in github.milestones():
#    milestone_id[m['title']] = m['number']
#    logging.debug("milestone (open)   title=%s" % m['title'])
#for m in github.milestones(query='state=closed'):
#    milestone_id[m['title']] = m['number']
#    logging.debug("milestone (closed) title=%s" % m['title'])

# We have no way to set the milestone closed date in GH.
# The 'due' and 'completed' are long ints representing datetimes.

logging.info("Migrating Trac milestones to GitHub...")
milestones = trac.sql('SELECT name, description, due, completed FROM milestone')
for name, description, due, completed in milestones:
    name = name.strip()
    logging.debug("milestone name=%s due=%s completed=%s" % (name, due, completed))
    if name and name not in milestone_id:
        if completed:
            state = 'closed'
        else:
            state = 'open'
        milestone = {'title': name,
                     'state': state,
                     'description': description,
                     }
        if due:
            milestone['due_on'] = epoch_to_iso(due)
        logging.debug("milestone: %s" % milestone)
        gh_milestone = github.milestones(id_ = max(chain([0], milestone_id.values())) + 1, data=milestone)
        milestone_id[name] = gh_milestone['number']

# Copy Trac tickets to GitHub issues, keyed to milestones above

tickets = trac.sql('SELECT id, summary, description , owner, milestone, component, status, time, changetime, reporter, keywords, severity, priority, resolution, type FROM ticket ORDER BY id') # LIMIT 5
for tid, summary, description, owner, milestone, component, status, \
         created_at, updated_at, reporter, keywords, severity, priority, resolution, type in tickets:
    if options.component and options.component != component:
        continue
    logging.info("Ticket %d: %s" % (tid, summary))
    if description:
        description = description.strip()
    if milestone:
        milestone = milestone.strip()
    issue = {'title': summary}
    if description:
        issue['body'] = description
    if milestone:
        m = milestone_id.get(milestone)
        if m:
            issue['milestone'] = m
    # Dont add component as label - only one component in dest repo, so redundant
    #if component:
        #if component not in labels:
        #    # GitHub creates the 'url' and 'color' fields for us
        #    github.labels(data={'name': component})
        #    labels[component] = 'CREATED' # keep track of it so we don't re-create it
        #    logging.debug("adding component as new label=%s" % component)
        #issue['labels'] = [component]
	#issue['labels'] = [{'name' : componenet}]
    # We have to create/map Trac users to GitHub usernames before we can assign
    # them to tickets
    if status == 'closed':
        issue['state'] = 'closed'
    if owner:
        issue['assignee'] = author_mapping(owner)
    if reporter:
        issue['user'] = author_mapping(reporter)
    if created_at:
	issue['created_at'] = epoch_to_iso(created_at)
    if updated_at:
	issue['updated_at'] = epoch_to_iso(updated_at)
    issue['labels'] = []
    if keywords:
        issue['labels'].extend([{'name' : keyword} for keyword in keywords])
    if severity:
	issue['labels'].append({'name' : severity})
    if priority:
	issue['labels'].append({'name' : priority})
    if resolution:
	issue['labels'].append({'name' : resolution})
    if type:
        issue['labels'].append({'name' : type})
    # save issue
    github.issues(tid, data=issue)
    # Add comments
    comment_data = []
    comments = trac.sql('SELECT author, newvalue AS body FROM ticket_change WHERE field="comment" AND ticket=%s' % tid)
    for author, body in comments:
        body = body.strip()
        if body:
            # replace svn commit with git one
	    if options.checkout:
                # search for [12345], r12345 changeset formats
                body = re.sub(r'[r\[](\d+)[\W]', svn_to_git_mapper, body)
            # prefix comment with author as git doesn't keep them separate
            if author:
                body = "%s: %s" % (author, body)
            logging.debug('issue comment: %s' % body[:40]) # TODO: escape newlines
	    comment_data.append({'user' : author_mapping(author), 'body' : body})

    if comment_data:
        github.issue_comments(tid, data=comment_data)

trac.close()
