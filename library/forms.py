from __future__ import annotations

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.utils.text import slugify

from .models import Branch, Organization, PatronProfile, Plan

User = get_user_model()


class OrganizationSignupForm(UserCreationForm):
    """Self-serve library (tenant) signup: creates the owner account + org."""

    organization_name = forms.CharField(max_length=200, label="Library name")
    organization_slug = forms.SlugField(
        max_length=50, label="URL slug", help_text="Letters, numbers and hyphens."
    )
    email = forms.EmailField(required=True)
    plan = forms.ModelChoiceField(queryset=Plan.objects.none(), required=False)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["plan"].queryset = Plan.objects.filter(active=True, public=True)

    def clean_organization_slug(self):
        slug = slugify(self.cleaned_data["organization_slug"])
        if not slug:
            raise forms.ValidationError("Enter a valid slug.")
        if Organization.objects.filter(slug=slug).exists():
            raise forms.ValidationError("That URL slug is already taken.")
        return slug

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email


class PatronSettingsForm(forms.ModelForm):
    """Let a patron manage their own notification email, SMS, and channels."""

    CHANNEL_CHOICES = [("email", "Email"), ("sms", "SMS"), ("push", "Push")]
    notification_channels = forms.MultipleChoiceField(
        choices=CHANNEL_CHOICES,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Where should we send hold/loan notices?",
    )

    class Meta:
        model = PatronProfile
        fields = ["notification_email", "sms_number", "notification_channels", "home_branch"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["home_branch"].required = False
        if self.instance and self.instance.organization_id:
            self.fields["home_branch"].queryset = Branch.objects.filter(
                organization=self.instance.organization, active=True
            )

    def clean(self):
        cleaned = super().clean()
        channels = cleaned.get("notification_channels") or []
        if "sms" in channels and not cleaned.get("sms_number"):
            self.add_error("sms_number", "Add a mobile number to receive SMS.")
        return cleaned


class PatronRegistrationForm(UserCreationForm):
    """Self-service patron sign-up.

    Creates a ``User`` plus a matching :class:`PatronProfile`. When the library
    is not fixed (``require_org=True``, i.e. several tenants and no explicit
    choice) the patron must select their organization instead of being bound to
    an arbitrary default. The pickup branch is optional.
    """

    first_name = forms.CharField(max_length=150, required=False)
    last_name = forms.CharField(max_length=150, required=False)
    email = forms.EmailField(required=True)
    organization = forms.ModelChoiceField(queryset=Organization.objects.none(), required=False)
    home_branch = forms.ModelChoiceField(
        queryset=Branch.objects.none(), required=False, empty_label="No preferred branch"
    )

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def __init__(self, *args, organization=None, require_org=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.organization = organization
        self.require_org = require_org
        if require_org:
            self.fields["organization"].queryset = Organization.objects.filter(active=True)
            self.fields["organization"].required = True
            # Branch depends on the chosen org; offer all active branches and
            # validate the pairing in clean().
            self.fields["home_branch"].queryset = Branch.objects.filter(active=True)
        else:
            del self.fields["organization"]
            if organization is not None:
                self.fields["home_branch"].queryset = Branch.objects.filter(
                    organization=organization, active=True
                )
            else:
                self.fields["home_branch"].disabled = True

    def clean_email(self):
        email = self.cleaned_data["email"]
        if email and User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("An account with this email already exists.")
        return email

    def clean(self):
        cleaned = super().clean()
        org = cleaned.get("organization") or self.organization
        branch = cleaned.get("home_branch")
        if branch and org and branch.organization_id != org.id:
            self.add_error("home_branch", "That branch does not belong to the selected library.")
        return cleaned


class CatalogImportUploadForm(forms.Form):
    """Librarian CSV upload for the staged catalog import pipeline."""

    csv_file = forms.FileField(
        label="CSV file",
        help_text=(
            "Columns: title (required), subtitle, authors, subjects, isbn_13, isbn_10, "
            "publisher, publication_year, format, branch, barcode, shelf_code, condition."
        ),
    )

    def clean_csv_file(self):
        upload = self.cleaned_data["csv_file"]
        name = (upload.name or "").lower()
        if not name.endswith((".csv", ".txt")):
            raise forms.ValidationError("Please upload a .csv file.")
        if upload.size and upload.size > 5 * 1024 * 1024:
            raise forms.ValidationError("File is too large (max 5 MB).")
        return upload
