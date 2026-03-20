from django.conf import settings
from django.db import models


class NewsItem(models.Model):
    league = models.ForeignKey(
        'core.League',
        on_delete=models.CASCADE,
        related_name='news_items',
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    show_date = models.DateField()
    expiration_date = models.DateField(blank=True, null=True)

    class Meta:
        ordering = ['-show_date', '-id']

    def clean(self):
        super().clean()
        if self.expiration_date and self.expiration_date < self.show_date:
            from django.core.exceptions import ValidationError
            raise ValidationError({
                'expiration_date': 'Expiration date must be on or after the show date.',
            })

    def __str__(self):
        return self.title


class Rule(models.Model):
    class RuleType(models.TextChoices):
        MAJOR_HEADING = 'major_heading', 'Major Heading'
        MINOR_HEADING = 'minor_heading', 'Minor Heading'
        RULE_ENTRY = 'rule_entry', 'Rule Entry'

    league = models.ForeignKey(
        'core.League',
        on_delete=models.CASCADE,
        related_name='rules',
    )
    text = models.TextField()
    rule_type = models.CharField(
        max_length=20,
        choices=RuleType.choices,
    )
    order = models.PositiveIntegerField()

    class Meta:
        ordering = ['order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['league', 'order'],
                name='unique_rule_order_per_league',
            ),
        ]

    def __str__(self):
        return f'{self.league} - {self.get_rule_type_display()} #{self.order}'


class RuleAuditLog(models.Model):
    class Action(models.TextChoices):
        ADDED = 'added', 'Added'
        EDITED = 'edited', 'Edited'
        DELETED = 'deleted', 'Deleted'

    rule = models.ForeignKey(
        Rule,
        on_delete=models.SET_NULL,
        related_name='audit_logs',
        null=True,
        blank=True,
    )
    league = models.ForeignKey(
        'core.League',
        on_delete=models.CASCADE,
        related_name='rule_audit_logs',
    )
    action = models.CharField(
        max_length=10,
        choices=Action.choices,
    )
    rule_order = models.PositiveIntegerField(null=True, blank=True)
    rule_type = models.CharField(max_length=20, blank=True)
    text = models.TextField(blank=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='rule_audit_logs',
    )
    changed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-changed_at', '-id']

    def __str__(self):
        return f'{self.get_action_display()} - {self.league} - {self.changed_at}'