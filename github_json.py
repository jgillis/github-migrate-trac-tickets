import base64
import urllib2
import os
try:
    import json
except ImportError:
    import simplejson as json


class GitHubJson(Object):
    """Dump json format suitable for import, See
         https://gist.github.com/7f75ced1fa7576412901
	 http://developer.github.com/v3/
    """
    def __init__(self, repo):
        """Username and password for auth; repo is like 'myorg/myapp'.
        """
        self.repo = repo

    def issues(self, id_, data=None):
        """Get issues or POST and issue with data.
        Create a new one like:    issues(data={'title': 'Plough', 'body': 'Plover'})
        """
	with open(os.path.join(self.repo, str(id_), '.json') as outfile:
          json.dump(data, outfile)
	return data

    def issue_comments(self, id_, data=None):
        """Get comments for a ticket by its number or POST a comment with data.
        Example: issue_comments(5, data={'body': 'Is decapitated'})
        """
	with open(os.path.join(self.repo, str(id_), 'comments.json')) as outfile:
	  json.dump(data, outfile)
	return data

    def milestones(self, id_, data=None):
        """Set milestones
        """
	with open(os.path.join(self.repo, 'milestones', str(id_), '.json') as outfile:
	    json.dump(data, outfile)
	return data

