import unittest
from unittest.mock import MagicMock, patch

import bridge_auth_probe as probe


class StrictResponse:
    def __init__(self, status):
        self.status_code = status
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    @property
    def content(self): raise AssertionError('body must not be read')
    @property
    def text(self): raise AssertionError('body must not be read')
    def json(self): raise AssertionError('body must not be parsed')
    def iter_content(self, *args, **kwargs): raise AssertionError('body must not be read')


class AuthProbeTests(unittest.TestCase):
    def test_positive_and_negative_status_only_no_body_or_redirect(self):
        session = MagicMock()
        responses = []
        for status in [401, 200]:
            response = StrictResponse(status)
            responses.append(response)
        session.get.side_effect = responses
        session.__enter__.return_value = session
        with patch.object(probe.requests, 'Session', return_value=session):
            self.assertTrue(probe.probe('http://127.0.0.1:8080/api', 'synthetic')['auth_enforced'])
        self.assertFalse(session.trust_env)
        for call in session.get.call_args_list:
            self.assertTrue(call.kwargs['stream']); self.assertFalse(call.kwargs['allow_redirects'])
        for response in responses:
            self.assertTrue(response.closed)
        self.assertEqual(session.get.call_args_list[0].kwargs['headers'], {})
        self.assertEqual(session.get.call_args_list[1].kwargs['headers'], {'Authorization': 'Bearer synthetic'})

    def test_auth_off_is_not_misreported_as_success(self):
        session = MagicMock(); session.__enter__.return_value = session
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        with patch.object(probe.requests, 'Session', return_value=session):
            self.assertFalse(probe.probe('http://127.0.0.1:8080/api', 'synthetic')['auth_enforced'])
        self.assertEqual(session.get.call_count, 1)  # No credential sent to auth-off endpoint.

    def test_no_existing_token_or_unapproved_target_makes_no_http_call(self):
        with patch.object(probe.requests, 'Session') as session:
            for url, token in [('https://foreign/api', 'synthetic'), ('http://127.0.0.1:8080/api', '')]:
                with self.assertRaises(ValueError): probe.probe(url, token)
            session.assert_not_called()
