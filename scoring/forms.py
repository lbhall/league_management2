from django import forms
from django.contrib.auth.models import User
from django.contrib.auth.password_validation import validate_password

from core.models import League, Player


class PlayerChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, player):
        team_name = player.team.name if player.team_id else 'No team'
        return f'{team_name} — {player.name}'


class SignupForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={'autocomplete': 'email', 'inputmode': 'email'}),
    )
    password1 = forms.CharField(
        label='Password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    password2 = forms.CharField(
        label='Confirm password',
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}),
    )
    player = PlayerChoiceField(
        queryset=Player.objects.none(),
        label='Who are you?',
        help_text='Pick your name from your team roster.',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['player'].queryset = (
            Player.objects.filter(
                league__results_type=League.ResultsType.EIGHT_BALL,
                team__isnull=False,
            )
            .select_related('team', 'league')
            .order_by('team__name', 'name')
        )

    def clean_email(self):
        email = self.cleaned_data['email'].lower().strip()
        if User.objects.filter(username__iexact=email).exists():
            raise forms.ValidationError('An account with this email already exists.')
        return email

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get('password1')
        password2 = cleaned.get('password2')
        if password1 and password2 and password1 != password2:
            self.add_error('password2', 'Passwords do not match.')
        elif password1:
            validate_password(password1)
        return cleaned


class LoginForm(forms.Form):
    # Accepts an email (captain accounts) or a plain Django username
    # (admin accounts), so no EmailField validation here.
    email = forms.CharField(
        label='Email or username',
        widget=forms.TextInput(attrs={'autocomplete': 'username'}),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={'autocomplete': 'current-password'}),
    )
