"""
REST client for the Globus Online identity management service (Globus Nexus).

Supports three methods of authentication:
    - username and password
    - oauth (deprecated)
    - goauth (new oauth based authentication service)

Username and password may be appropriate for testing, but goauth is the
recommended auth method going forward. Use nexus.client.NexusClient
request_client_credential or get_access_token_from_code to obtain an
access token.

TODO: refactor
"""
__author__ = 'Mattias Lidman'

import logging
import json
import urllib
import httplib2
import time
from oauth2 import Request as OAuthRequest
from oauth2 import SignatureMethod_HMAC_SHA1, Consumer, generate_nonce

import base64
from datetime import datetime
import hashlib
from subprocess import Popen, PIPE
import urlparse

import yaml
import nexus.token_utils as token_utils
from nexus.utils import (
        read_openssh_public_key,
        read_openssh_private_key,
        canonical_time,
        b64encode,
        sha1_base64,
        sign_with_rsa)
import requests
import rsa

log = logging.getLogger(__name__)

class GlobusOnlineRestClient(object):
    # NOTE: GraphRestClient would be more accurate, but if we want to release
    # this publically GlobusOnlineRestClient is probably a more pedagogical name.

    def __init__(self, config=None, config_file=None):
        if config_file is not None:
            with open(config_file, 'r') as cfg:
                self.config = yaml.load(cfg.read())
        elif config is not None:
            self.config = config
        else:
            raise ValueError("No configuration was specified")
        self.server = self.config['server']
        cache_config = self.config.get('cache', {
                    'class': 'nexus.token_utils.InMemoryCache',
                    'args': [],
                    })
        # client is the current user that the GlobusOnlineRestClient is using
        # but not necessarily acting as.
        self.user_key_file = self.config.get('user_private_key_file', '~/.ssh/id_rsa')
        cache_class = cache_config['class']
        self.verify_ssl = self.config.get('verify_ssl', True)
        mod_name = '.'.join(cache_class.split('.')[:-1])
        mod = __import__(mod_name)
        for child_mod_name in mod_name.split('.')[1:]:
            mod = getattr(mod, child_mod_name)
        cache_impl_class = getattr(mod, cache_class.split('.')[-1])
        self.cache = cache_impl_class(*cache_config.get('args', []))
        self.cache = token_utils.LoggingCacheWrapper(self.cache)

        # Initial login supported either using username+password or
        # username+oauth_secret. The client also supports unauthenticated calls.
        self.session_cookies = {}
        self.default_password = 'sikrit' # Don't tell anyone.
        self.client = self.oauth_secret = self.goauth_token = None
        username = self.config['client']
        oauth_secret = self.config.get('oauth_secret', None)
        goauth_token = self.config.get('goauth_token', None)
        password = self.config['client_secret']
        self.client_secret = password
        if username:
            if oauth_secret:
                self.username_oauth_secret_login(username, oauth_secret)
            elif goauth_token:
                self.username_goauth_token_login(username, goauth_token)
            else:
                self.username_password_login(username, password=password)


    # GROUP OPERATIONS
    def get_group_list(self, my_roles=None, my_statuses=None):
        # Get the groups list resource.
        # Filtering supported by passing a list of roles (admin/manager/member) as
        # my_roles or a list of statuses (active/invited/pending/suspended/rejected)
        # as my_statuses. Negative filtering supported by prepending a status or role
        # with '!'.
        group_filter = self._construct_group_filter(my_roles, my_statuses)
        url = '/groups?' + group_filter
        return self._issue_rest_request(url)

    def get_group_tree(self, root_gid, depth, my_roles=None, my_statuses=None):
        # Get the groups tree resource.
        # Filtering supported by passing a list of roles (admin/manager/member) as
        # my_roles or a list of statuses (active/invited/pending/suspended/rejected)
        # as my_statuses. Negative filtering supported by prepending a status or role
        # with '!'.
        group_filter = self._construct_group_filter(my_roles, my_statuses)
        url = '/groups/' + root_gid + '/tree?depth=' + str(depth) + '&' + group_filter
        return self._issue_rest_request(url)

    def get_group_summary(self, gid):
        url = '/groups/' + gid
        return self._issue_rest_request(url)

    def get_group_members(self, gid):
        url = '/groups/' + gid + '/members'
        return self._issue_rest_request(url)

    def get_group_member(self, gid, username):
        url = '/groups/' + gid + '/members/' + username
        return self._issue_rest_request(url)

    def get_group_policies(self, gid):
        url = '/groups/' + gid + '/policies'
        return self._issue_rest_request(url)

    def get_group_email_templates(self, gid):
        # Returned document does not include the message of each template.
        # Use get_group_email_template for that.
        url = '/groups/' + gid + '/email_templates'
        return self._issue_rest_request(url)

    def get_group_email_template(self, gid, template_id):
        # Get a single email template, including message. The template_id can
        # be gotten by using get_group_email_templates.
        url = '/groups/' + gid + '/email_templates/' + template_id
        return self._issue_rest_request(url)

    def get_rendered_group_email_template(self, gid, template_id):
        url = '/groups/' + gid + '/email_templates/' + template_id
        return self._issue_rest_request(url, params={'mode': 'view'})

    def post_group(self, name, description=None, parent=None):
        # Create a new group.
        if not description:
            description = 'A group called "' + name + '"'
        params = { 'name': name, 'description': description}
        if parent:
            params['parent'] = parent
        return self._issue_rest_request('/groups/', http_method='POST', params=params)

    def put_group_summary(self, gid, name=None, description=None):
        # Edit group. Group name and description are the only things that
        # can be set using this method.
        url = '/groups/' + gid
        params = {}
        if name:
            params['name'] = name
        if description:
            params['description'] = description
        return self._issue_rest_request(url, http_method='PUT', params=params)

    def put_group_policies(self, gid, policies):
        # PUT policies in dict policies. Utility function build_policy_dictionary()
        # may be used to simplify building the document.
        url = '/groups/' + gid + '/policies'
        return self._issue_rest_request(url, http_method='PUT', params=policies)

    def set_single_policy(self, gid, policy, new_policy_options):
        # Wrapper function for easily setting a single policy. For a given policy,
        # all policy options specified in new_policy_options are set to true,
        # all others to false. new_policy_options may be a string for single-value
        # policies and must be a list for multi-value policies.
        if type(new_policy_options) == str:
            new_policy_options = [new_policy_options]

        response, policies = self.get_group_policies(gid)
        existing_policy_options = policies[policy]['value']

        for policy_option in existing_policy_options.keys():
            if policy_option in new_policy_options:
                existing_policy_options[policy_option]['value'] = True
            else:
                existing_policy_options[policy_option]['value'] = False
        policies[policy]['value'] = existing_policy_options

        return self.put_group_policies(gid, policies)

    def post_group_email_templates(self, gid, params):
        # Create one or more new email templates.
        url = '/groups/' + gid + '/email_templates'
        return self._issue_rest_request(url, http_method='POST', params=params)

    def put_group_email_template(self, gid, template_id, params):
        # Update an email template.
        url = '/groups/' + gid + '/email_templates/' + template_id
        return self._issue_rest_request(url, http_method='PUT', params=params)

    # GROUP MEMBERSHIP OPERATIONS

    def post_membership(self, gid, usernames=None, emails=None):
        # POSTing a membership corresponds to inviting a user identified by a
        # username or an email address to a group, or requesting to join a group
        # (if the actor is among the listed usernames).
        url = '/groups/' + gid + '/members'
        params = {}
        if usernames:
            if type(usernames) == str:
                usernames = [ usernames ]
            params['users'] = usernames
        if emails:
            if type(emails) == str:
                emails = [ emails ]
            params['emails'] = emails
        return self._issue_rest_request(url, http_method='POST', params=params)

    def put_group_membership(self, gid, username, email, role, status, status_reason,
            last_changed=None, user_details=None):
        # PUT is used for accepting invitations and making other changes to a membership.
        # The document is validated against the following schema:
        # https://raw.github.com/globusonline/goschemas/integration/member.json
        # membership_id == invite_id for purposes of accepting an invitation.

        url = '/groups/' + gid + '/members/' + username
        return self._put_group_membership(url, username, email, role, status,
            status_reason, user_details)

    def put_group_membership_by_id(self, invite_id, username, email, role, status,
            status_reason, last_changed=None, user_details=None):
        # put_group_membership_by_id() is used for tying an email invite to a GO user,
        # use put_group_membership() otherwise.
        url = '/memberships/' + membership_id
        return self._put_group_membership(url, username, email, role, status,
            status_reason, user_details)


    def put_group_membership_role(self, gid, username, new_role):
        response, member = self.get_group_member(gid, username)
        member['role'] = new_role
        return self.put_group_membership(
            gid,
            username,
            member['email'],
            member['role'],
            member['status'],
            member['status_reason'])

    def claim_invitation(self, invite_id):
        # claim_invitation ties an email invite to a GO user, and must be done
        # before the invite can be accepted.
        url = '/memberships/' + invite_id
        response, user = self.get_user(self.client)
        response, membership = self._issue_rest_request(url)
        membership['username'] = user['username']
        membership['email'] = user['email']
        params = {
            'username' : user['username'],
            'status' : membership['status'],
            'status_reason' : membership['status_reason'],
            'role' : membership['role'],
            'email' : user['email'],
            'last_changed' : '2007-03-01T13:00:00',
        }

        return self._issue_rest_request(url, http_method='PUT', params=params)



    def accept_invitation(self, gid, username, status_reason=None):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'pending',
            'invited',
            'Only invited users can accept an invitation.')

    def reject_invitation(self, gid, username, status_reason=None):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'rejected',
            'invited',
            'Only an invited user can reject an invitation.')

    def reject_pending(self, gid, username, status_reason=None):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'rejected',
            'pending',
            'Only possible to reject membership for pending users.')

    def approve_join(self, gid, username, status_reason=None):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'active',
            'pending',
            'Only invited users can accept an invitation.')

    def suspend_group_member(self, gid, username, new_status_reason=''):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'suspended',
            'active',
            'Only active members can be suspended.',
            new_status_reason)

    def unsuspend_group_member(self, gid, username, new_status_reason=''):
        return self._put_membership_status_wrapper(
            gid,
            username,
            'active',
            'suspended',
            'Only suspended members can be unsuspended.',
            new_status_reason)

    def delete_group(self, gid):
        path = '/groups/' + gid
        return self._issue_rest_request(path, 'DELETE')

    # USER OPERATIONS

    def get_user(self, username, fields=None, custom_fields=None, use_session_cookies=False):
        # If no fields are explicitly set the following will be returned by Graph:
        # ['fullname', 'email', 'username', 'email_validated', 'system_admin', 'opt_in']
        # No custom fields are returned by default.
        query_params = {}
        if fields:
            query_params['fields'] = ','.join(fields)
        if custom_fields:
            query_params['custom_fields'] = ','.join(custom_fields)
        url = '/users/' + username + '?' + urllib.urlencode(query_params)
        return self._issue_rest_request(url, use_session_cookies=use_session_cookies)

    def get_user_secret(self, username, use_session_cookies=False):
        # Gets the secret used for OAuth authentication.
        return self.get_user(username, fields=['secret'], use_session_cookies=use_session_cookies)

    def get_user_profile(self, username):
        url = '/users/' + username + '/profile'
        return self._issue_rest_request(url)

    def post_user(self, username, fullname, email, password, **kwargs):
        # Create a new user.

        accept_terms = True if not kwargs.has_key('accept_terms') else kwargs['accept_terms']
        opt_in = True if not kwargs.has_key('opt_in') else kwargs['opt_in']

        params = { 'username': username, 'fullname': fullname, 'email': email,
            'password': password, 'accept_terms' : accept_terms, 'opt_in': opt_in }

        return self._issue_rest_request('/users', 'POST', params=params)

    def put_user(self, username, **kwargs):
        # Edit existing user.
        kwargs['username'] = username
        path = '/users/' + username

        return self._issue_rest_request(path, 'PUT', params = kwargs)

    def put_user_custom_fields(self, username, **kwargs):
        response, content = self.get_user(username)
        content['custom_fields'] = kwargs
        content.pop('username')
        return self.put_user(username, **content)

    def get_user_policies(self, username):
        url = '/users/' + username + '/policies'
        return self._issue_rest_request(url)

    def put_user_policies(self, username, policies):
        url = '/users/' + username + '/policies'
        return self._issue_rest_request(url, http_method='PUT', params=policies)

    def put_user_membership_visibility(self, username, new_visibility):
        response, policies = self.get_user_policies(username)
        visibility_policy = policies['user_membership_visibility']['value']
        for policy_option in visibility_policy.keys():
            visibility_policy[policy_option]['value'] = policy_option == new_visibility
        policies['user_membership_visibility']['value']
        response, content = self.put_user_policies(username, policies)
        return response, content

    def delete_user(self, username):
        path = '/users/' + username
        return self._issue_rest_request(path, 'DELETE')

    def username_password_login(self, username, password=None):
        # After successful username/password authentication the user's OAuth secret
        # is retrieved and used in all subsequent calls until the user is logged out.
        # If no username is provided, authentication will be attempted using the default
        # password used by the simple_create_user() method.
        path = '/authenticate'
        if not password:
            password = self.default_password
        params = {'username': username, 'password': password}
        response, content = self._issue_rest_request(path, http_method='POST',
            params=params, use_session_cookies=True)
        if response['status'] != '200':
            return response, content
        # Also get user secret so that subsequent calls can be made using OAuth:
        secret_response, secret_content = self.get_user_secret(username, use_session_cookies=True)
        if secret_response['status'] != '200':
            raise UnexpectedRestResponseError(
                "Could not retrieve user secret.")
        self.oauth_secret = secret_content['secret']
        self.client = username
        self.session_cookies = None
        return response, content

    def username_oauth_secret_login(self, username, oauth_secret):
        # login_username_oauth_secret() tries to retrieve username's user object
        # using the provided oauth_secret. If succesfull, the username and
        # oauth_secret will be used for all subsequent calls until user is logged
        # out. The result of the get_user() call is returned.
        old_oauth_secret = self.oauth_secret
        old_client = self.client
        self.oauth_secret = oauth_secret
        self.client = username
        response, content = self.get_user(username)
        if response['status'] != '200':
            self.oauth_secret = old_oauth_secret
            self.client = old_client
        return response, content

    def username_goauth_token_login(self, username, goauth_token):
        old_goauth_token = self.goauth_token
        old_client = self.client
        self.goauth_token = goauth_token
        self.client = username
        response, content = self.get_user(username)
        if response['status'] != '200':
            self.goauth_token = old_goauth_token
            self.client = old_client
        return response, content

    # NOTE: It might make sense going forward to restrict each GlobusOnlineRestClient
    # object to a single user. goauth_get_access_token_from_code() doesn't handle
    # logging out and logging in as a different user very well because it uses the
    # client_secret (password). The client_secret is hard to track between logins
    # because oauth and goauth login methods don't require a client_secret
    def logout(self):
        response, content = self._issue_rest_request('/logout')
        self.client = None
        self.session_cookies = None
        self.oauth_secret = None
        self.goauth_token = None
        self.client_secret = None
        return response, content

    def post_email_validation(self, validation_code):
        url = '/validation'
        params = {'validation_code': validation_code}
        return self._issue_rest_request(url, http_method='POST', params=params)

    def post_rsa_key(self, key_name, rsa_key=None, rsa_key_file=None):
        if rsa_key_file is not None:
             with open(rsa_key_file, 'r') as key_file:
                 key = key_file.readline()
        elif rsa_key is not None:
             key = rsa_key
        else:
            raise ValueError("No rsa key was specified")

        path = '/users/'+self.client+'/credentials/ssh2'
        params = {'alias': key_name, 'ssh_key': key}
        return self._issue_rest_request(path, http_method='POST', params=params)

    def get_rsa_key_list(self):
        path = '/users/'+self.client+'/credentials'
        return self._issue_rest_request(path)

    def delete_rsa_key(self, credential_id):
        path = '/users/'+self.client+'/credentials/ssh2/'+credential_id
        return self._issue_rest_request(path, http_method='DELETE')

    # UTILITY FUNCTIONS

    def build_policy_dictionary(self, **kwargs):
        # Each kwargs must be a dictionary named after a policy, containing policy
        # options and values. For example:
        #    approval = { 'admin': True, 'auto_if_admin': False, 'auto': False, }
        # go_rest_client_tests.py contains an example setting all policies available
        # as of this writing.
        policies = {}
        for policy in kwargs.keys():
            policy_options = {}
            policy_options_source = kwargs[policy]
            for option_key in kwargs[policy].keys():
                policy_options[option_key] = {
                    'value': kwargs[policy][option_key]
                }
            policies[policy] = {
                'value': policy_options
            }
        return policies

    def _construct_group_filter(self, my_roles, my_statuses):
        params = {}
        statuses = set(['active', 'invited', 'pending', 'suspended', 'rejected',
            '!active', '!invited', '!pending', '!suspended', '!rejected'])
        roles = set(['admin', 'manager', 'member', '!admin', '!manager', '!member'])
        if my_roles:
            if len(set(my_roles) - roles) != 0:
                raise ValueError('Invalid roles:' + str(set(my_roles) - roles))
            params['my_roles'] = ",".join(my_roles)
        if my_statuses:
            if len(set(my_statuses) - statuses) != 0:
                raise ValueError('Invalid statuses:' + str(set(my_statuses) - statuses))
            params['my_statuses'] = ",".join(my_statuses)
        return urllib.urlencode(params)

    def _issue_rest_request(self, path, http_method='GET', content_type='application/json',
        accept='application/json', params=None, use_session_cookies=False):

        http = httplib2.Http(disable_ssl_certificate_validation=True, timeout=10)

        url = 'https://' + self.server + path
        headers = {}
        headers['Content-Type'] = content_type
        headers['Accept'] = accept
        # Use OAuth authentication, session cookies, or no authentication?
        if use_session_cookies:
            if self.session_cookies:
                headers['Cookie'] = self.session_cookies
        elif self.client and self.oauth_secret:
            auth_headers = self._get_auth_headers(http_method, url)
            # Merge dicts. In case of a conflict items in headers take precedence.
            headers = dict(auth_headers.items() + headers.items())
        elif self.client and self.goauth_token:
            headers["Authorization"] = "Globus-Goauthtoken %s" \
                                       % self.goauth_token
        body = None
        if params:
            if content_type == 'application/x-www-form-urlencoded':
                body = urllib.urlencode(params)
            else:
                body = json.dumps(params)
        response, content = http.request(url, http_method, headers=headers, body=body)
        if response.has_key('set-cookie'):
            self.session_cookies = response['set-cookie']
        if 'content-type' in response and 'application/json' in response['content-type'] and content != '':
            return response, json.loads(content)
        else:
            return response, {}

    def _get_auth_headers(self, method, url):
        oauth_params = {
            'oauth_version': "1.0",
            'oauth_nonce': generate_nonce(),
            'oauth_timestamp': int(time.time())
        }
        oauth_request = OAuthRequest(method, url, parameters=oauth_params)
        consumer = Consumer(self.client, self.oauth_secret)
        oauth_request.sign_request(SignatureMethod_HMAC_SHA1(), consumer, None)
        auth_headers = oauth_request.to_header()
        auth_headers['Authorization'] = auth_headers['Authorization'].encode('utf-8')
        return auth_headers

    def _put_group_membership(self, url, username, email, role, status, status_reason,
            user_details=None):
        params = {
            'username': username,
            'status': status,
            'status_reason': status_reason,
            'role': role,
            'email': email,
        }
        # last_changed needs to be set or validation will fail, but the value
        # will get overwritten by Graph anyway.
        params['last_changed'] = '2007-03-01T13:00:00'
        if user_details:
            params['user'] = user_details
        return self._issue_rest_request(url, http_method='PUT', params=params)

    def _put_membership_status_wrapper(self, gid, username, new_status, expected_current,
            transition_error_message, new_status_reason=''):
        response, member = self.get_group_member(gid, username)
        if member['status'] != expected_current:
            raise StateTransitionError(member['status'], new_status,
                transition_error_message)
        member['status'] = new_status
        member['status_reason'] = new_status_reason
        return self.put_group_membership(
            gid,
            username,
            member['email'],
            member['role'],
            member['status'],
            member['status_reason'])

    def goauth_validate_token(self, token):
        """
        Validate that a token was issued for the specified user and client by
        the server in the SigningSubject.

        :param token: An authentication token provided by the client.

        :return: username, client id and the server that issued the token.

        :raises ValueError: If the signature is invalid, the token is expired or
        the public key could not be gotten.
        """
        return token_utils.validate_token(token, self.cache, self.verify_ssl)


    def goauth_generate_request_url(self, username=None):
        """
        In order for the user to authorize the client to access his data, he
        must first go to the custom url provided here.

        :param username: (Optional) This will pre-populate the user's info in the form

        :return: A custom authorization url
        """
        query_params = {
                "response_type": "code",
                "client_id": self.client,
                }
        if username is not None:
            query_params['username'] = username
        parts = ('https', self.server, '/goauth/authorize',
                urllib.urlencode(query_params), None)
        return urlparse.urlunsplit(parts)

    def goauth_get_access_token_from_code(self, code):
        """
        After receiving a code from the end user, this method will acquire an
        access token from the server which can be used for subsequent requests.

        :param code: The code which the user received after authenticating with the server and authorizing the client.

        :return: Tuple containing (access_token, refresh_token, expire_time)
        """
        url_parts = ('https', self.server, '/goauth/token', None, None)
        result = token_utils.request_access_token(self.client,
                self.client_secret, code, urlparse.urlunsplit(url_parts))
        return (
                result.access_token,
                result.refresh_token,
                time.mktime(datetime.utcnow().timetuple()) + result.expires_in
                )

    def goauth_rsa_get_request_token(self, username, client_id, password=None):
        query_params = {
                "response_type": "code",
                "client_id": client_id
                }
        query_params = urllib.urlencode(query_params)
        path = '/goauth/authorize'
        method = 'GET'
        headers = sign_with_rsa(self.user_key_file,
                path,
                method,
                username,
                query=query_params,
                password=password)
        url_parts = ('https', self.server, '/goauth/authorize', query_params, None)
        url = urlparse.urlunsplit(url_parts)
        response = requests.get(url, headers=headers, verify=self.verify_ssl)
        return response.json()

    def goauth_request_client_credential(self, client_id, password=None):
        """
        This is designed to support section 4.4 of the OAuth 2.0 spec:

        "The client can request an access token using only its client
         credentials (or other supported means of authentication) when the
         client is requesting access to the protected resources under its
         control"
        """
        body = 'grant_type=client_credentials'
        path = '/goauth/token'
        method = 'POST'
        headers = sign_with_rsa(self.user_key_file,
                path,
                method,
                client_id,
                body=body,
                password=password)
        url_parts = ('https', self.server, path, None, None)
        url = urlparse.urlunsplit(url_parts)
        response = requests.post(url, data={'grant_type': 'client_credentials'}, headers=headers, verify=self.verify_ssl)
        return response.json()

    def goauth_get_user_using_access_token(self, access_token):
        access_token_dict = dict(field.split('=') for field in access_token.split('|'))
        user_path = '/users/' + access_token_dict['un']
        url_parts = ('https', self.server, user_path, None, None)
        url = urlparse.urlunsplit(url_parts)
        headers = {
            "X-Globus-Goauthtoken": str(access_token),
            "Content-Type": "application/json"
        }
        response = requests.get(url, headers=headers, verify=self.verify_ssl)
        assert(response.status_code == requests.codes.ok)
        return response.json()

class StateTransitionError(Exception):
    def __init__(self, prev_state, next_state, message):
        self.message = "Can't transition from '" + prev_state + "' to '" + next_state + "'. " + message

    def __str__(self):
        return self.message

class UnexpectedRestResponseError(Exception):
    def __init__(self, message):
        self.message = message

    def __str__(self):
        return self.message
