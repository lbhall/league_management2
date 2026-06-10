from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver
from .models import FailedLogin

@receiver(user_login_failed)
def log_failed_login(sender, credentials, request, **kwargs):
    username = credentials.get('username')
    ip_address = None
    user_agent = None

    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(',')[0].strip()
        else:
            ip_address = request.META.get('REMOTE_ADDR')
        
        user_agent = request.META.get('HTTP_USER_AGENT')

    FailedLogin.objects.create(
        username=username,
        ip_address=ip_address,
        user_agent=user_agent
    )
