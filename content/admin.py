from django.contrib import admin

from .models import NewsItem, Rule, RuleAuditLog


@admin.register(NewsItem)
class NewsItemAdmin(admin.ModelAdmin):
    list_display = ('title', 'league', 'show_date', 'expiration_date')
    search_fields = ('title', 'description')

    def get_list_filter(self, request):
        if request.user.is_superuser:
            return ('league', 'show_date')
        return ('show_date',)


@admin.register(Rule)
class RuleAdmin(admin.ModelAdmin):
    list_display = ('league', 'order', 'rule_type', 'text')
    search_fields = ('text',)
    list_filter = ('league', 'rule_type')
    ordering = ('league', 'order', 'id')

    def save_model(self, request, obj, form, change):
        action = RuleAuditLog.Action.EDITED if change else RuleAuditLog.Action.ADDED
        super().save_model(request, obj, form, change)

        RuleAuditLog.objects.create(
            rule=obj,
            league=obj.league,
            action=action,
            rule_order=obj.order,
            rule_type=obj.rule_type,
            text=obj.text,
            changed_by=request.user,
        )

    def delete_model(self, request, obj):
        RuleAuditLog.objects.create(
            rule=None,
            league=obj.league,
            action=RuleAuditLog.Action.DELETED,
            rule_order=obj.order,
            rule_type=obj.rule_type,
            text=obj.text,
            changed_by=request.user,
        )
        super().delete_model(request, obj)


@admin.register(RuleAuditLog)
class RuleAuditLogAdmin(admin.ModelAdmin):
    list_display = ('league', 'action', 'rule_order', 'rule_type', 'changed_by', 'changed_at')
    search_fields = ('text',)
    list_filter = ('league', 'action', 'rule_type', 'changed_at')
    ordering = ('-changed_at', '-id')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False