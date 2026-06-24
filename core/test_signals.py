from django.contrib.auth.signals import user_login_failed
from django.test import RequestFactory, TestCase

from core.models import FailedLogin


class FailedLoginSignalTests(TestCase):
    def test_logs_ip_from_x_forwarded_for(self):
        request = RequestFactory().post('/admin/login/')
        request.META['HTTP_X_FORWARDED_FOR'] = '1.2.3.4, 5.6.7.8'
        request.META['HTTP_USER_AGENT'] = 'test-agent'

        user_login_failed.send(
            sender=None, credentials={'username': 'baduser'}, request=request,
        )

        entry = FailedLogin.objects.get(username='baduser')
        self.assertEqual(entry.ip_address, '1.2.3.4')
        self.assertEqual(entry.user_agent, 'test-agent')

    def test_logs_ip_from_remote_addr_without_forwarded_header(self):
        request = RequestFactory().post('/admin/login/')
        request.META['REMOTE_ADDR'] = '9.9.9.9'

        user_login_failed.send(
            sender=None, credentials={'username': 'baduser2'}, request=request,
        )

        entry = FailedLogin.objects.get(username='baduser2')
        self.assertEqual(entry.ip_address, '9.9.9.9')

    def test_handles_missing_request(self):
        user_login_failed.send(
            sender=None, credentials={'username': 'baduser3'}, request=None,
        )

        entry = FailedLogin.objects.get(username='baduser3')
        self.assertIsNone(entry.ip_address)
        self.assertIsNone(entry.user_agent)
