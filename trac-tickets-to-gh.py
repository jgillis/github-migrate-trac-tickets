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

from datetime import datetime
# TODO: conditionalize and use 'json'
import logging
from optparse import OptionParser
import sqlite3
import re
from getpass import getpass
from itertools import chain
import subprocess
from collections import defaultdict, namedtuple

from github import GitHub

Repository = namedtuple('Repository', 'name type rev_map_file')

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

class RevisionMapping(object):
    def __init__(self, repo_list):
        self.mappings = {}
        self.rx_revlink = re.compile(r'((^|\s)r|\[)(?P<id>([0-9a-f]{6,40})|([0-9]+))(/(?P<suffix>\w+))?\]?')
        for repo in repo_list:
            self.mappings[repo.name] = {}
            logging.info('Reading revision mapping for "{0}" from {1}'.format(repo.name, repo.rev_map_file))
            with open(repo.rev_map_file, 'r') as f:
                for line in f.readlines():
                    src_id, git_id = line.split(' => ')
                    self.mappings[repo.name][src_id] = git_id
                    if repo.type == 'hg' or repo.type == 'git':
                        # Add shorter keys for abbreviated SHA1 links.
                        # This consumes more memory space, but it would not be a problem
                        # on modern computers.
                        for i in range(6, 12):
                            self.mappings[repo.name][src_id[:i]] = git_id

    def _sub(self, match):
        src_id = match.group('id')
        repo_suffix = match.group('suffix')
        if repo_suffix is None:
            repo_suffix = ''
        heuristic_tried = defaultdict(bool)
        while True:
            try:
                mapping = self.mappings[repo_suffix]
            except KeyError:
                raise ValueError('Unspecified repository suffix: {0}'.format(repo_suffix))
            try:
                git_id = mapping[src_id]
            except KeyError:
                # ----- Textcube-specific -----
                if src_id.isdigit() and not heuristic_tried['digit-is-svn']:
                    repo_suffix = 'old_svn'
                    heuristic_tried['digit-is-svn'] = True
                    continue
                # ----- End of Textcube-specific ----
                return match.group(0)
            else:
                return git_id

    def convert(self, text):
        return self.rx_revlink.sub(self._sub, text)

def epoch_to_iso(x):
    iso_ts = datetime.fromtimestamp(x / 1e6).isoformat()
    return iso_ts

# Warning: optparse is deprecated in python-2.7 in favor of argparse
if __name__ == '__main__':
    usage = """
      %prog [options] trac_db_path github_username github_repo

      The path might be something like "/tmp/trac.db"
      The github_repo combines user or organization and specific repo like "myorg/myapp"

      To test on local machines, use --json flag and give fake github username and repository path.
      You must delete the target path if it already exists.
    """
    parser = OptionParser(usage=usage)
    parser.add_option('-q', '--quiet', action='store_true', default=False,
                      help='Decrease logging of activity (default: false)')
    parser.add_option('-c', '--component', default=None,
                      help='Component to migrate (default: all)')
    parser.add_option('-j', '--json', action='store_true', default=False,
                      help='Output to json files for github import (default: direct upload)')
    parser.add_option('--authors-file', default=None,
                      help='Author mapping file, if not specified take usernames from trac as given')
    parser.add_option('--revmap-files', default=None,
                      help='Comma-separated list of revision mapping files')
    parser.add_option('--repo-types', default=None,
                      help='Comma-separated list of repository types')
    parser.add_option('--repo-names', default=None,
                      help='Comma-separated list of repository names (empty string means the default one)')
    parser.add_option('-y', '--dry-run', action='store_true', default=False,
                      help='Do not actually post to GitHub, but only show the conversion result. (default: false)')

    (options, args) = parser.parse_args()
    try:
        trac_db_path, github_username, github_repo = args
    except ValueError:
        parser.error('Wrong number of arguments')
    if not '/' in github_repo:
        parser.error('Repo must be specified like "organization/project"')

    if options.quiet:
        logging.basicConfig(level=logging.INFO)
    else:
        logging.basicConfig(level=logging.DEBUG)

    if not options.revmap_files:
        parser.error('You must specify at least one revision mapping file. (--revmap-files)')
    if not options.repo_types:
        parser.error('You must specify at least one source repo type. (--repo-types)')
    if not options.repo_names:
        parser.error('You must specify at least one source repo name. (--repo-names)')
    options.revmap_files = options.revmap_files.split(',')
    options.repo_names = options.repo_names.split(',')
    options.repo_types = options.repo_types.split(',')
    assert len(options.repo_names) == len(options.repo_types)
    assert len(options.repo_names) == len(options.revmap_files)
    repo_list = []
    for i, name in enumerate(options.repo_names):
        repo_list.append(Repository(name, options.repo_types[i], options.revmap_files[i]))

    trac = Trac(trac_db_path)
    if options.json:
        from github_json import GitHubJson
        github = GitHubJson(github_repo, dry_run=options.dry_run)
    else:
        github_password = getpass('Password for user {0}: '.format(github_username))
        github = GitHub(github_username, github_password, github_repo,
                        dry_run=options.dry_run)

    # default to no mapping
    author_mapping = AuthorMapping(options.authors_file)
    rev_mapping = RevisionMapping(repo_list)

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
            if options.dry_run:
                continue
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
            issue['body'] = rev_mapping.convert(description)
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
        # Save issue
        github.issues(tid, data=issue)
        # Add comments
        comment_data = []
        comments = trac.sql('SELECT author, newvalue, time AS body FROM ticket_change WHERE field="comment" AND ticket=%s' % tid)
        for author, body, timestamp in comments:
            body = body.strip()
            if body:
                # Replace the commit ID with git one
                body = rev_mapping.convert(body)
                # prefix comment with author as git doesn't keep them separate
                if author:
                    body = "%s: %s" % (author, body)
                if timestamp:
                    timestamp = epoch_to_iso(timestamp)
                logging.debug(u'  comment: {0}'.format(body[:70].replace(u'\r\n', u'\\n').replace(u'\n', u'\\n')))
                comment_data.append({'user' : author_mapping(author), 'body' : body, \
                                     'created_at' : timestamp, 'updated_at' : timestamp})

        if comment_data:
            github.issue_comments(tid, data=comment_data)

    trac.close()
