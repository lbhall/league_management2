from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from content.models import NewsItem, Rule, RuleAuditLog
from core.models import League


def make_league(**kwargs):
    defaults = {
        'name': 'Content Admin League',
        'team_size': 1,
        'results_type': League.ResultsType.ONE_POCKET,
        'day_of_week': League.DayOfWeek.MONDAY,
    }
    defaults.update(kwargs)
    return League.objects.create(**defaults)


class ContentAdminTestCase(TestCase):
    def setUp(self):
        User = get_user_model()
        self.superuser = User.objects.create_superuser(
            username='admin', password='password123', email='admin@example.com',
        )
        self.client.login(username='admin', password='password123')
        self.league = make_league()


class NewsItemAdminTests(ContentAdminTestCase):
    def test_changelist_renders_for_superuser(self):
        NewsItem.objects.create(
            league=self.league, title='Hello', description='World', show_date='2026-01-01',
        )
        response = self.client.get(reverse('admin:content_newsitem_changelist'))
        self.assertEqual(response.status_code, 200)


class RuleAdminTests(ContentAdminTestCase):
    def test_adding_a_rule_creates_audit_log_entry(self):
        response = self.client.post(reverse('admin:content_rule_add'), {
            'league': self.league.id,
            'text': 'No slow play.',
            'rule_type': Rule.RuleType.RULE_ENTRY,
            'order': 1,
        })
        self.assertEqual(response.status_code, 302)
        rule = Rule.objects.get(text='No slow play.')
        log = RuleAuditLog.objects.get(rule=rule)
        self.assertEqual(log.action, RuleAuditLog.Action.ADDED)
        self.assertEqual(log.changed_by, self.superuser)

    def test_editing_a_rule_creates_audit_log_entry(self):
        rule = Rule.objects.create(
            league=self.league, text='Original', rule_type=Rule.RuleType.RULE_ENTRY, order=1,
        )
        response = self.client.post(reverse('admin:content_rule_change', args=[rule.pk]), {
            'league': self.league.id,
            'text': 'Updated text',
            'rule_type': Rule.RuleType.RULE_ENTRY,
            'order': 1,
        })
        self.assertEqual(response.status_code, 302)
        log = RuleAuditLog.objects.filter(rule=rule, action=RuleAuditLog.Action.EDITED).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.text, 'Updated text')

    def test_deleting_a_rule_creates_audit_log_entry(self):
        rule = Rule.objects.create(
            league=self.league, text='To Delete', rule_type=Rule.RuleType.RULE_ENTRY, order=1,
        )
        response = self.client.post(
            reverse('admin:content_rule_delete', args=[rule.pk]), {'post': 'yes'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Rule.objects.filter(pk=rule.pk).exists())
        log = RuleAuditLog.objects.filter(action=RuleAuditLog.Action.DELETED, text='To Delete').first()
        self.assertIsNotNone(log)
        self.assertIsNone(log.rule)


class RuleAuditLogAdminTests(ContentAdminTestCase):
    def test_cannot_add_audit_log_entries(self):
        response = self.client.get(reverse('admin:content_ruleauditlog_add'))
        self.assertEqual(response.status_code, 403)

    def test_cannot_actually_save_changes_to_audit_log_entries(self):
        # GET on the change view is allowed read-only (Django superusers always
        # pass has_view_permission), but has_change_permission=False blocks the
        # actual POST that would save a change.
        log = RuleAuditLog.objects.create(
            league=self.league, action=RuleAuditLog.Action.ADDED, text='x',
        )
        response = self.client.get(reverse('admin:content_ruleauditlog_change', args=[log.pk]))
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            reverse('admin:content_ruleauditlog_change', args=[log.pk]),
            {'text': 'changed', 'action': RuleAuditLog.Action.ADDED, 'league': self.league.id},
        )
        self.assertEqual(response.status_code, 403)

    def test_changelist_renders(self):
        RuleAuditLog.objects.create(league=self.league, action=RuleAuditLog.Action.ADDED, text='x')
        response = self.client.get(reverse('admin:content_ruleauditlog_changelist'))
        self.assertEqual(response.status_code, 200)
