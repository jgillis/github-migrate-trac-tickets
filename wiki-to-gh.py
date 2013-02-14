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
from git import *
import codecs
from subprocess import *

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
                    match = re.compile(r'^([\(\w\s\@&\.\d]*) = ([\(\d\w ]*) <(.*)>$').search(line)
                    if not match:
                        raise ValueError, 'Author line not in correct format: "%s"' % line
                    svn_user, github_user, email = match.groups()
                    self.mapping[svn_user.strip()] = {"login" : github_user.strip(), "mail": email.strip()}
                # Ignore email for now, seems username is eough

    def __call__(self, username):
        if not self.mapping:
                return {"login" : username,"mail": "None"}
        # just take 1st user if given a list
        username = username.split(',')[0].strip()
        if not username in self.mapping:
        #    print "%s = DMWMBot <USER@DOMAIN>" % username
            return {"login" : "None","mail": "None"}
        # Throw if author not in mapping
        return self.mapping[username]

class RevisionMapping(object):
    def __init__(self, repo_list):
        self.mappings = {}
        self.rx_revlink = re.compile(r'((^|\s)r|(c|C)ommit |(r|R)evision |\[)(?P<id>([0-9a-f]{6,40})|([0-9]+))(/(?P<suffix>\w+))?\]?')
        for repo in repo_list:
            self.mappings[repo.name] = {}
            logging.info('Reading revision mapping for "{0}" from {1}'.format(repo.name, repo.rev_map_file))
            with open(repo.rev_map_file, 'r') as f:
                for line in f.readlines():
                    src_id, git_id = line.split(' => ')
                    git_id = git_id.rstrip()
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
            repo_suffix = 'main'
        heuristic_tried = defaultdict(bool)
        while True:
            try:
                mapping = self.mappings[repo_suffix]
            except KeyError:
                raise ValueError('Unspecified repository suffix: {0}'.format(repo_suffix))
            try:
                git_id = mapping[src_id]
            except KeyError:
                return match.group(0)
            else:
                return git_id

    def convert(self, text):
        return self.rx_revlink.sub(self._sub, text)

def epoch_to_iso(x):
    iso_ts = datetime.fromtimestamp(x).isoformat()
    return iso_ts

def convert_wikiformat(text,rev_mapping=None,title=False,mainpage=False):
    pieces = []
    in_pre = False
    text = text.replace("\\r","")
    text = text.replace('\\"','"')
    # Links
    in_pre_trigger = False
    for line in text.splitlines():
        line = re.sub("^ *(\w)",r"\1",line)
        if line.startswith(u'=') and not in_pre:
            depth = len(line.split(u' ')[0])
            line = u'#' * depth + line.strip(u'=')
        elif line.startswith(u'{{{') and not '}}' in line:
            line = "```"  + ("\n" if len(line.lstrip('{'))!=0 else "") + line.lstrip('{')
            in_pre = True
        elif line.endswith(u'}}}') and not '{{{' in line:
            line = u'' + line.rstrip('}') + ("\n" if len(line.rstrip('}'))!=0 else "") + "```"
            in_pre_trigger = True
        if in_pre:
            line = line.replace('\\\\','\\')
        else:
            line = line.replace('^    *',' *')
            if not(title):
              line = line.replace('>','\\>')
              line = line.replace('<','\\<')
            if rev_mapping is not None:
              line = rev_mapping.convert(line)
            if mainpage:
              line = re.sub("\[wiki:([^\] ]*?) ([^\]]*?)\]",r"[\2](wiki/\1)",line)
            else:
              line = re.sub("\[wiki:([^\] ]*?) ([^\]]*?)\]",r"[\2](\1)",line)
            line = re.sub("\[wiki:(.*?)\]",r"[[\1]]",line)
            line = re.sub("\[(http[^\] ]*?) ([^\]]*?)\]",r"[\2](\1)",line)
            line = re.sub("\[(http.*?)\]",r"\1",line)
            line = re.sub("\[\[Image\((.*?)\)\]\]",r"![](\1)<br/>",line)
            if line.endswith("::"): line=line[:-1]

            def repl(m):
              branch = "master" if m.group("R") is None else rev_mapping.convert("["+m.group("R")+"]")
              suffix = "" if m.group("L") is None else "#L" + m.group("L")
              return "[" + m.group("C") + "](/casadi/casadi/blob/"+branch+"/"+m.group("C")+suffix+")"
            line = re.sub("source:/trunk/(?P<C>[^@#\\b]*)(@(?P<R>\d+))?(#L(?P<L>[\d-]+))?",repl,line)

            line = re.sub("{{{(.*?)}}}?",r"`\1`",line) # inline escape
            def repl(m):
              s = m.group(1)
              s = s.replace('\\>','>')
              s = s.replace('\\<','<')
              return "`"+s+"`"
            line = re.sub("`(.*?)`",repl,line) 
            line = line.replace('[[br]]', '<br>')
            line = line.replace('[[BR]]', '<br>')
            line = re.sub("''' *", '**',line)
            line = re.sub(" *'''", '**',line)
            line = re.sub(" *''", '*',line)
            line = re.sub("'' *", '*',line)
        if in_pre_trigger:
          in_pre = False
          in_pre_trigger = False
        pieces.append(line)
        
    result = u'\n'.join(pieces)
    result = re.sub("```\n#!(.*)",r"```\1",result)
    return result

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
        trac_db_path, wiki_repo_path = args
    except ValueError:
        parser.error('Wrong number of arguments')

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

    # default to no mapping
    author_mapping = AuthorMapping(options.authors_file)
    rev_mapping = RevisionMapping(repo_list)
    
    tickets = trac.sql('SELECT name,version,time,author,ipnr,text,comment,readonly FROM wiki ORDER BY time') # LIMIT 5
    for name,version,time,author,ipnr,text,comment,readonly in tickets:
      if name=="WikiStart": name="Home"
      if name.startswith("Trac") or name.startswith("Wiki"): continue
      if name in ["CamelCase","InterMapTxt","InterTrac","InterWiki","PageTemplates","RecentChanges","SandBox","TitleIndex"]: continue
      if comment is None: comment=""
      if text is None: text=""
      out = codecs.open(wiki_repo_path + '/' + name + '.md','w','utf-8')
      out.write(convert_wikiformat(text,rev_mapping=rev_mapping,mainpage=name=="Home"))
      p=Popen(['git','add',name + '.md'],cwd=wiki_repo_path)
      p.wait()
      p=Popen(['git','commit','--allow-empty-message','--author="'+author_mapping(author)['login']+' <'+ author_mapping(author)['mail'] +'>"','--date='+epoch_to_iso(time),'-m',convert_wikiformat(comment,rev_mapping=rev_mapping)],cwd=wiki_repo_path)
      p.wait()
      print name

    trac.close()
