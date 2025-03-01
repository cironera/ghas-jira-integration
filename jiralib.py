from jira import JIRA
import re
import util
import logging
import requests
import json

# JIRA Webhook events
UPDATE_EVENT = "jira:issue_updated"
CREATE_EVENT = "jira:issue_created"
DELETE_EVENT = "jira:issue_deleted"


TITLE_PREFIXES = {
    "Alert": "[Code Scanning Alert]:",
    "Secret": "[Secret Scanning Alert]:",
}

DESC_TEMPLATE = """
{long_desc}

{alert_url}

Repository: {repo_id}
Alert #: {alert_num}

This issue was automatically generated from a GitHub alert, and will be automatically resolved once the underlying problem is fixed.
"""


STATE_ISSUE_SUMMARY = "[Code Scanning Issue States]"
STATE_ISSUE_KEY = util.make_key("gh2jira-state-issue")
STATE_ISSUE_TEMPLATE = """
This issue was automatically generated and contains states required for the synchronization between GitHub and JIRA.
DO NOT MODIFY DESCRIPTION BELOW LINE.
ISSUE_KEY={issue_key}
""".format(
    issue_key=STATE_ISSUE_KEY
)

logger = logging.getLogger(__name__)


class Jira:
    def __init__(self, url, user, token):
        self.url = url
        self.user = user
        self.token = token
        self.j = JIRA(url, basic_auth=(user, token))

    def auth(self):
        return self.user, self.token

    def getProject(self, projectkey, endstate, reopenstate, labels):
        return JiraProject(self, projectkey, endstate, reopenstate, labels)

    def list_hooks(self):
        resp = requests.get(
            "{api_url}/rest/webhooks/1.0/webhook".format(api_url=self.url),
            headers={"Content-Type": "application/json"},
            auth=self.auth(),
            timeout=util.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        for h in resp.json():
            yield h

    def create_hook(
        self,
        name,
        url,
        secret,
        events=[CREATE_EVENT, DELETE_EVENT, UPDATE_EVENT],
        filters={"issue-related-events-section": ""},
        exclude_body=False,
    ):
        data = json.dumps(
            {
                "name": name,
                "url": url + "?secret_token=" + secret,
                "events": events,
                "filters": filters,
                "excludeBody": exclude_body,
            }
        )
        resp = requests.post(
            "{api_url}/rest/webhooks/1.0/webhook".format(api_url=self.url),
            headers={"Content-Type": "application/json"},
            data=data,
            auth=self.auth(),
            timeout=util.REQUEST_TIMEOUT,
        )
        resp.raise_for_status()

        return resp.json()


class JiraProject:
    def __init__(self, jira, projectkey, endstate, reopenstate, labels):
        self.jira = jira
        self.labels = labels.split(",") if labels else []
        self.projectkey = projectkey
        self.j = self.jira.j
        self.endstate = endstate
        self.reopenstate = reopenstate

    def get_state_issue(self, issue_key="-"):
        if issue_key != "-":
            return self.j.issue(issue_key)

        issue_search = 'project={jira_project} and description ~ "{key}"'.format(
            jira_project='"{}"'.format(self.projectkey), key=STATE_ISSUE_KEY
        )
        issues = list(
            filter(
                lambda i: i.fields.summary == STATE_ISSUE_SUMMARY,
                self.j.search_issues(issue_search, maxResults=0),
            )
        )

        if len(issues) == 0:
            return self.j.create_issue(
                project=self.projectkey,
                summary=STATE_ISSUE_SUMMARY,
                description=STATE_ISSUE_TEMPLATE,
                issuetype={"name": "Alert"},
                labels=self.labels,
            )
        elif len(issues) > 1:
            issues.sort(key=lambda i: i.id())  # keep the oldest issue
            for i in issues[1:]:
                i.delete()

        i = issues[0]

        # When fetching issues via the search_issues() function, we somehow
        # cannot access the attachments. To do that, we need to fetch the issue
        # via the issue() function first.
        return self.j.issue(i.key)

    def fetch_repo_state(self, repo_id, issue_key="-"):
        i = self.get_state_issue(issue_key)

        for a in i.fields.attachment:
            if a.filename == repo_id_to_fname(repo_id):
                return util.state_from_json(a.get())

        return {}

    def save_repo_state(self, repo_id, state, issue_key="-"):
        i = self.get_state_issue(issue_key)

        # remove previous state files for the given repo_id
        for a in i.fields.attachment:
            if a.filename == repo_id_to_fname(repo_id):
                self.j.delete_attachment(a.id)

        # attach the new state file
        self.jira.attach_file(
            i.key, repo_id_to_fname(repo_id), util.state_to_json(state)
        )

    def create_issue(
        self,
        repo_id,
        short_desc,
        long_desc,
        alert_url,
        alert_type,
        alert_num,
        repo_key,
        alert_key,
        severity,
    ):
        raw = self.j.create_issue(
            project=self.projectkey,
            summary="{prefix} {short_desc} in {repo}".format(
                prefix=TITLE_PREFIXES[alert_type], short_desc=short_desc, repo=repo_id
            ),
            description=DESC_TEMPLATE.format( #edit this down to minimum
                long_desc=long_desc,
                alert_url=alert_url,
                repo_id=repo_id,
                alert_type=alert_type,
                alert_num=alert_num,
                repo_key=repo_key,
                alert_key=alert_key,
            ),
            issuetype={"name": "Alert"},
            labels=self.labels,
            customfield_10454={"id": "20301"},              #Source = GitHub
            customfield_10907={"id": "20303"},              #Alert Type = Code Scanning Alert
            customfield_10235=repo_id,                      #Reported Products
            customfield_10909=repo_key,                     #Alert Reference Key
            customfield_10910=alert_key,                    #Alert Key
            customfield_10284=alert_url,                    #External Related Link
            customfield_10908={"id": severity},             #Severity
        )
        logger.info(
            "Created issue {issue_key} for alert {alert_num} in {repo_id}.".format(
                issue_key=raw.key, alert_num=alert_num, repo_id=repo_id
            )
        )
        logger.info(
            "Created issue {issue_key} for {alert_type} {alert_num} in {repo_id}.".format(
                issue_key=raw.key,
                alert_type=alert_type,
                alert_num=alert_num,
                repo_id=repo_id,
            )
        )

        return JiraIssue(self, raw)

    def fetch_issues(self, key):
        #heres where we tweak the search to look in new fields, not in the description
        issue_search = 'project={jira_project} and "Alert Reference Key" ~ "{key}"'.format(
            jira_project='"{}"'.format(self.projectkey), key=key
        )
        issues = list(
            filter(
                lambda i: i.is_managed(),
                [
                    JiraIssue(self, raw)
                    for raw in self.j.search_issues(issue_search, maxResults=0)
                ],
            )
        )
        logger.debug(
            "Search {search} returned {num_results} results.".format(
                search=issue_search, num_results=len(issues)
            )
        )
        return issues


class JiraIssue:
    def __init__(self, project, rawissue):
        self.project = project
        self.rawissue = rawissue
        self.j = self.project.j
        self.endstate = self.project.endstate
        self.reopenstate = self.project.reopenstate
        self.labels = self.project.labels

    def is_managed(self):
        if parse_alert_info(self.rawissue.fields)[0] is None:  #change to send fields
            return False
        return True

    def get_alert_info(self):
        return parse_alert_info(self.rawissue.fields)

    def key(self):
        return self.rawissue.key

    def id(self):
        return self.rawissue.id

    def delete(self):
        #update here - cannot delete, but we can change the summary to start with [DELETE] - and we should probably remove the alert reference key too... (TBD)
        logger.info("Mark issue {ikey} for deletion.".format(ikey=self.key()))
        if not self.rawissue.fields.summary.startswith('[DELETE]'):
            self.rawissue.update(summary="[DELETE] {}".format(self.rawissue.fields.summary))

    def get_state(self):
        return self.parse_state(self.rawissue.fields.status.name)

    def adjust_state(self, state):
        if state:
            self.transition(self.reopenstate)
        else:
            self.transition(self.endstate)

    def parse_state(self, raw_state):
        return raw_state != self.endstate

    def transition(self, transition):

        if (
            self.get_state()
            and transition == self.reopenstate
            or not self.get_state()
            and transition == self.endstate
        ):
            # nothing to do
            return
            
        jira_transitions = {
            t["name"]: t["id"] for t in self.j.transitions(self.rawissue)
        }

        #need to do this due to transition names being different than the state names
        transname = transition
        if transition == self.endstate:
            transname = "Force Close" 
        elif transition == self.reopenstate:
            transname = "Reopen"

        if transname not in jira_transitions:
            logger.error(
                'Transition "{transition}" not available for {issue_key}. Valid transitions: {jira_transitions}'.format(
                    transition=transition,
                    issue_key=self.rawissue.key,
                    jira_transitions=list(jira_transitions),
                )
            )
            raise Exception("Invalid JIRA transition")

        if transition == self.endstate:
            self.j.transition_issue(self.rawissue, 'Force Close',  fields={ 'customfield_10061':'Resolved by GHAS to Jira sync'}, comment='Closed by GitHub to Jira sync')
        else:
            self.j.transition_issue(self.rawissue, 'Reopen', comment='Reopened by GitHub to Jira sync')

        action = "Reopening" if transition == self.reopenstate else "Closing"

        logger.info(
            "{action} issue {issue_key}".format(
                action=action, issue_key=self.rawissue.key
            )
        )

    def persist_labels(self, labels):
        if labels:
            self.rawissue.update(fields={"labels": self.labels})


def parse_alert_info(fields):   #change to accept rawissue.fields
    """
    Parse all the fields in an issue's description and return
    them as a tuple. If parsing fails for one of the fields,
    return a tuple of None's.
    """
    failed = None, None, None, None
    if fields.customfield_10235 is None:
        return failed
    repo_id = fields.customfield_10235

    if fields.customfield_10907 is None:
        alert_type = None
    else:
        alert_type = fields.customfield_10907
    
    m = re.search("([^\/]+$)", fields.customfield_10284) # pulling from url #number of issue from jira - like "RALEMANDSO-667"
    if m is None:
        return failed
    alert_num = int(m.group(1))

    if fields.customfield_10909 is None:
        return failed
    repo_key = fields.customfield_10909

    if fields.customfield_10910 is None:
        return failed
    alert_key = fields.customfield_10910 

    return repo_id, alert_num, repo_key, alert_key, alert_type


def repo_id_to_fname(repo_id):
    return repo_id.replace("/", "^") + ".json"
