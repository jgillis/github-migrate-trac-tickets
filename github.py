import base64
import urllib2
import logging
try:
    import json
except ImportError:
    import simplejson as json

class AlreadyExists(RuntimeError):
    pass
class DoesNotExist(RuntimeError):
    pass

class GitHub(object):
    """Connections, queries and posts to GitHub.
    """
    def __init__(self, username, password, repo, dry_run=False):
        """Username and password for auth; repo is like 'myorg/myapp'.
        """
        self.username = username
        self.password = password
        self.repo = repo
        self.dry_run = dry_run
        self.url = "https://api.github.com/repos/%s" % self.repo
        self.auth = base64.encodestring('%s:%s' % (self.username, self.password))[:-1]

    def access(self, path, query=None, data=None):
        """Append the API path to the URL GET, or POST if there's data.
        """
        logger = logging.getLogger(__name__)
        if not path.startswith('/'):
            path = '/' + path
        if query:
            path += '?' + query
        url = self.url + path
        if self.dry_run and data is not None:
            return
        req = urllib2.Request(url)
        req.add_header("Authorization", "Basic %s" % self.auth)
        try:
            if data:
                req.add_header("Content-Type", "application/json")
                res = urllib2.urlopen(req, json.dumps(data))
            else:
                res = urllib2.urlopen(req)
            # TODO: check rate-limit by response headers: X-RateLimit-Limit & X-RateLimit-Remaining
            return json.load(res)
        except urllib2.HTTPError as e:
            if e.code == 422:
                err_info = json.loads(e.read())
                err_reason = err_info['errors'][0]['code']
                logger.debug('api validation error: {0}'.format(json.dumps(err_info)))
                if err_reason == 'already_exists':
                    raise AlreadyExists('Already exists: {0[resource]}, {0[field]}'.format(err_info['errors'][0]))
                elif err_reason == 'missing':
                    raise DoesNotExist('Missing resource: {0[resource]}'.format(err_info['errors'][0]))
                elif err_reason == 'invalid':
                    raise ValueError('Invalid field: {0[field]}'.format(err_info['errors'][0]))
                elif err_reason == 'missing_field':
                    raise ValueError('Missing field: {0[field]}'.format(err_info['errors'][0]))
            raise RuntimeError("HTTPERror on url=%s e=%s" % (url, e))
        except IOError as e:
            raise RuntimeError("IOError on url=%s e=%s" % (url, e))

    def issues(self, id_=None, query=None, data=None):
        """Get issues or POST and issue with data.
        Query for specifics like: issues(query='state=closed')
        Create a new one like:    issues(data={'title': 'Plough', 'body': 'Plover'})
        You ca NOT set the 'number' param and force a GitHub issue number.
        """
        path = 'issues'
        if id_:
            path += '/' + str(id_)
        return self.access(path, query=query, data=data)

    def issue_comments(self, id_, query=None, data=None):
        """Get comments for a ticket by its number or POST a comment with data.
        Example: issue_comments(5, data={'body': 'Is decapitated'})
        """
        # This call has no way to get a single comment
        #TODO: this is BROKEN
        return self.access('issues/%d/comments' % id_, query=query, data=data)

    def labels(self, query=None, data=None):
        """Get labels or POST a label with data.
        Post like: labels(data={'name': 'NewLabel'})
        """
        return self.access('labels', query=query, data=data)

    def milestones(self, query=None, data=None):
        """Get milestones or POST if data.
        Post like: milestones(data={'title':'NEWMILESTONE'})
        There are many other attrs you can set in the API.
        """
        return self.access('milestones', query=query, data=data)

