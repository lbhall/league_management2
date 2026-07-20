from django.contrib import admin

from .models import ScoringProfile


@admin.register(ScoringProfile)
class ScoringProfileAdmin(admin.ModelAdmin):
    list_display = ('user', 'league', 'player', 'role', 'is_approved', 'created_at')
    list_filter = ('league', 'role', 'is_approved')
    search_fields = ('user__username', 'user__email', 'player__name')
    actions = ['approve_profiles']

    @admin.action(description='Approve selected scoring accounts')
    def approve_profiles(self, request, queryset):
        updated = queryset.update(is_approved=True)
        self.message_user(request, f'Approved {updated} account(s).')
