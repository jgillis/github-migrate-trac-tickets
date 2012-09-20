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

from github import GitHub, AlreadyExists, DoesNotExist

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
        logging.basicConfig(level=logging.INFO, format='%(levelname)9s: %(message)s')
    else:
        logging.basicConfig(level=logging.DEBUG, format='%(levelname)9s: %(message)s')

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

    def parse_keywords(keywords):
        if isinstance(keywords, tuple):
            keywords = ','.join(keywords)
        if ',' in keywords:
            keywords = map(lambda k: k.strip(), keywords.split(','))
        else:
            keywords = keywords.split(' ')
        for kwd in keywords:
            if not kwd.strip():
                continue
            yield kwd

    # Get GitHub labels; we'll merge Trac components into them
    logging.info("Getting existing GitHub labels...")
    gh_labels = set()
    for label in github.labels():
        gh_labels.add(label['name'])
    logging.info("Getting the set of labels in Trac....")
    trac_labels = set()
    trac_label_colors = {
        'resolution': {
            'fixed': '228b22',
            'wontfix': 'bebebe',
            'duplicate': 'c8c8c8',
            'invalid': 'aaaaaa',
            'worksforme': '008c8c',
            'reqconfirm': 'e16a9d',
        },
        'priority': {
            'blocker': '800000',
            'critical': 'a52a2a',
            'major': 'b22222',
            'minor': 'b90000',
            'trivial': 'cd5c5c',
        },
        'severity': {
            # Textcube did not use this.
        },
        'ticket_type': {
            'defect': '9400d3',
            'enhancement': '0064ff',
        },
    }
    trac_label_types = {}
    for type_, name, value in trac.sql("SELECT type, name, value FROM enum"):
        if name == 'defect': continue  # exception: this is mapped to "bug"
        trac_label_types[name] = type_  # reverse mapping
        trac_labels.add(name)
    # Keywords in Textcube Trac has no clean rules and are too diverged.
    # We won't migrate them.
    #for keywords in trac.sql("SELECT keywords FROM ticket"):
    #    if keywords is None:
    #        continue
    #    for kwd in parse_keywords(keywords):
    #        trac_labels.add(kwd)
    for name in (gh_labels | trac_labels):
        logging.debug(u"label name={0}".format(name))
    logging.info("Adding undefine labels to GitHub...")
    # Add (undefined) labels
    for name in (trac_labels - gh_labels):
        try:
            color = trac_label_colors[trac_label_types[name]][name]
        except KeyError:
            color = 'e8e8e8'
        github.labels(data={
            'name': name,
            'color': color,
        })

    # == Milestone Migration ==
    # Get any existing GitHub milestones so we can merge Trac into them.
    # We need to reference them by numeric ID in tickets.
    logging.info("Getting existing GitHub milestones...")
    milestone_id = {}
    for m in github.milestones():
        milestone_id[m['title']] = m['number']
        logging.debug("milestone (open)   title={0}".format(m['title']))
    # API returns only 'open' issues by default, have to ask for closed like:
    # curl -u 'USER:PASS' https://api.github.com/repos/USERNAME/REPONAME/milestones?state=closed
    for m in github.milestones(query='state=closed'):
        milestone_id[m['title']] = m['number']
        logging.debug("milestone (closed) title={0}".format(m['title']))

    # We have no way to set the milestone closed date in GitHub.
    # The 'due' and 'completed' are long ints representing datetimes.
    logging.info("Migrating Trac milestones to GitHub...")
    milestones = trac.sql('SELECT name, description, due, completed FROM milestone')
    for name, description, due, completed in milestones:
        name = name.strip()
        if name in milestone_id:
            logging.warn("milestone {0} already exists; using it instead of migrated one.".format(name))
            continue
        logging.debug("milestone {0} due={1} completed={2}".format(name, due, completed))
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
            logging.debug("milestone: {0}".format(milestone))
            if options.dry_run:
                continue
            try:
                gh_milestone = github.milestones(data=milestone)
                milestone_id[name] = gh_milestone['number']
            except AlreadyExists:
                # NOTE: Unfortunately, API does not return the "number"
                #       property of the duplicate.  We work-around this problem
                #       by prefetching existing milestone objects above.
                pass

    # == Ticket Migration ==
    tickets = trac.sql('SELECT id, summary, description, owner, milestone, component, status, time, changetime, reporter, keywords, severity, priority, resolution, type FROM ticket ORDER BY id') # LIMIT 5
    for tid, summary, description, owner, milestone, component, status, \
             created_at, updated_at, reporter, keywords, severity, priority, resolution, type_ in tickets:
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
        # Don't add component as label -- only one component in dest repo, so redundant
        #if component:
        #    if component not in labels:
        #        # GitHub creates the 'url' and 'color' fields for us
        #        github.labels(data={'name': component})
        #        labels[component] = 'CREATED' # keep track of it so we don't re-create it
        #        logging.debug("adding component as new label=%s" % component)
        #    issue['labels'] = [component]
        #    issue['labels'] = [{'name' : componenet}]
        # We have to create/map Trac users to GitHub usernames before we can assign
        # them to tickets
        if owner:
            issue['assignee'] = author_mapping(owner)['login']
        issue['labels'] = []
        # We don't migrate keywords and did not use severity.
        #if keywords:
        #    for keyword in parse_keywords(keywords):
        #        issue['labels'].append({'name': keyword})
        #if severity:
        #    issue['labels'].append({'name': severity})
        if priority:
            issue['labels'].append({'name': priority})
        if resolution:
            issue['labels'].append({'name': resolution})
        if type_:
            if type_ == 'defect':
                type_ = 'bug'  # convert to GH's default label.
            issue['labels'].append({'name': type_})

        issue['body'] += u'\n\n<ul><li>이슈 등록시간: {0}</li>\n'.format(epoch_to_iso(created_at))
        issue['body'] += u'<li>마지막 수정시간: {0}</li></ul>\n'.format(epoch_to_iso(updated_at))

        # Add comments
        comment_data = [u'<table class="trac-migrated-comments">']
        comments = trac.sql('SELECT author, newvalue, time AS body FROM ticket_change WHERE field="comment" AND ticket=%s' % tid)
        for author, body, timestamp in comments:
            body = body.strip()
            if body:
                body = rev_mapping.convert(body)
                if timestamp:
                    timestamp = epoch_to_iso(timestamp)
                logging.debug(u'  comment: {0}'.format(body[:70].replace(u'\r\n', u'\\n').replace(u'\n', u'\\n')))
                # Don't worry about escaping--GitHub will handle these with Markdown formatter.
                comment_data.append(u'<tr><th style="text-align:left">Comment by {0} at {1}</th></tr>'.format(
                    author_mapping(author)['login'], timestamp
                ))
                comment_data.append(u'<tr><td>{0}</td></tr>'.format(body))
        comment_data.append(u'</table>')
        issue['body'] += u'\n' + u'\n'.join(comment_data)

        # Save the issue.
        # NOTE: we cannot set the issue number when creating.
        try:
            result = github.issues(data=issue)
            logging.debug('New issue no.: {0} => {1}'.format(tid, result['number']))
            if status == 'closed':
                # Unfortunately, we should use another query to close it.
                github.issues(result['number'], data={'state': 'closed'})
        except ValueError as e:
            logging.error(e)  # TEMPORARY
            continue

    trac.close()
