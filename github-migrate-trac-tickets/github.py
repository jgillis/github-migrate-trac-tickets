from github import Github

github = GitHub("git@github.com:casadi/casadi.git")

import requests
import json
r = requests.get('/user/emails')
        if(r.ok):
            repoItem = json.loads(r.text or r.content)
            print "Django repository created: " + repoItem['created_at']
