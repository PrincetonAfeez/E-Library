import hashlib
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import check_password, make_password
from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.db import models
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

User = get_user_model()


def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().casefold().split())


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class PublicStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    SUPPRESSED = "suppressed", "Suppressed"


class Organization(TimeStampedModel):
    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    default_timezone = models.CharField(max_length=64, default="UTC")
    active = models.BooleanField(default=True)
    # When enabled, staff with a confirmed TOTP device must pass a second factor
    # before reaching staff areas.
    require_staff_mfa = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Branch(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="branches"
    )
    name = models.CharField(max_length=200)
    slug = models.SlugField()
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, blank=True)
    active = models.BooleanField(default=True)
    loan_days = models.PositiveSmallIntegerField(default=21)
    hold_pickup_days = models.PositiveSmallIntegerField(default=7)
    max_renewals = models.PositiveSmallIntegerField(default=2)

    class Meta:
        ordering = ["organization__name", "name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_branch_slug_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.name} ({self.organization})"

    @property
    def effective_timezone(self) -> str:
        return self.timezone or self.organization.default_timezone


class ShelfLocation(TimeStampedModel):
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="shelf_locations")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=120)
    floor = models.CharField(max_length=80, blank=True)
    room = models.CharField(max_length=80, blank=True)
    section = models.CharField(max_length=80, blank=True)
    shelf = models.CharField(max_length=80, blank=True)
    public_label = models.CharField(max_length=200, blank=True)
    staff_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["branch__name", "code"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "code"], name="uniq_shelf_code_per_branch")
        ]

    def __str__(self) -> str:
        return self.public_label or f"{self.branch.name} {self.code}"


class PatronStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    BLOCKED = "blocked", "Blocked"
    ARCHIVED = "archived", "Archived"


class PatronProfile(TimeStampedModel):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="patron_profile")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="patrons")
    library_card_number = models.CharField(max_length=64)
    home_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="home_patrons", null=True, blank=True
    )
    status = models.CharField(
        max_length=16, choices=PatronStatus.choices, default=PatronStatus.ACTIVE
    )
    patron_type = models.ForeignKey(
        "PatronType", on_delete=models.SET_NULL, null=True, blank=True, related_name="patrons"
    )
    max_loans = models.PositiveSmallIntegerField(default=12)
    max_holds = models.PositiveSmallIntegerField(default=8)
    retain_loan_history = models.BooleanField(default=False)
    notification_email = models.EmailField(blank=True)
    sms_number = models.CharField(max_length=32, blank=True)
    push_token = models.CharField(max_length=255, blank=True)
    # Enabled delivery channels, e.g. ["email", "sms"]; empty == email only.
    notification_channels = models.JSONField(default=list, blank=True)
    # Per-category opt-in, e.g. {"courtesy": false}. Missing key == opted in.
    # Essential categories (overdue, holds) are always delivered regardless.
    notification_prefs = models.JSONField(default=dict, blank=True)
    # Global opt-out of non-essential notices (CAN-SPAM one-click unsubscribe).
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    unsubscribe_token = models.CharField(max_length=64, blank=True, db_index=True)

    class Meta:
        ordering = ["user__last_name", "user__first_name", "library_card_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "library_card_number"], name="uniq_card_per_org"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user.get_username()} ({self.library_card_number})"


class StaffRole(models.TextChoices):
    LIBRARIAN = "librarian", "Librarian"
    BRANCH_MANAGER = "branch_manager", "Branch Manager"
    ADMIN = "admin", "Admin"
    SUPPORT = "support", "Support"


class StaffMembership(TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="staff_memberships")
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="staff")
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="staff_memberships", null=True, blank=True
    )
    role = models.CharField(max_length=32, choices=StaffRole.choices)
    permissions = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["organization__name", "role", "user__username"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "organization", "branch", "role"],
                name="uniq_staff_membership_scope",
            )
        ]

    def __str__(self) -> str:
        scope = self.branch.name if self.branch else self.organization.name
        return f"{self.user.get_username()} {self.role} @ {scope}"


class Author(TimeStampedModel):
    name = models.CharField(max_length=200)
    normalized_name = models.CharField(max_length=220, db_index=True, blank=True)
    sort_name = models.CharField(max_length=220, blank=True)
    aliases = models.JSONField(default=list, blank=True)
    authority_identifier = models.CharField(max_length=120, blank=True)

    class Meta:
        ordering = ["sort_name", "name"]
        indexes = [models.Index(fields=["normalized_name"], name="author_normalized_idx")]

    def save(self, *args, **kwargs):
        self.normalized_name = normalize_text(self.name)
        if not self.sort_name:
            self.sort_name = self.name
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.name


class Subject(TimeStampedModel):
    name = models.CharField(max_length=160)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True)
    public = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Work(TimeStampedModel):
    canonical_title = models.CharField(max_length=300)
    subtitle = models.CharField(max_length=300, blank=True)
    normalized_title = models.CharField(max_length=340, db_index=True, blank=True)
    slug = models.SlugField(unique=True)
    summary = models.TextField(blank=True)
    authors = models.ManyToManyField(Author, related_name="works", blank=True)
    subjects = models.ManyToManyField(Subject, related_name="works", blank=True)
    public_status = models.CharField(
        max_length=16, choices=PublicStatus.choices, default=PublicStatus.PUBLISHED
    )
    internal_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["canonical_title"]
        indexes = [models.Index(fields=["normalized_title"], name="work_normalized_title_idx")]

    def save(self, *args, **kwargs):
        self.normalized_title = normalize_text(self.canonical_title)
        if not self.slug:
            self.slug = slugify(self.canonical_title)
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.canonical_title

    def get_absolute_url(self):
        return reverse("work_detail", kwargs={"slug": self.slug})


class EditionFormat(models.TextChoices):
    HARDCOVER = "hardcover", "Hardcover"
    PAPERBACK = "paperback", "Paperback"
    EBOOK = "ebook", "Ebook"
    AUDIOBOOK = "audiobook", "Audiobook"
    LARGE_PRINT = "large_print", "Large Print"


class Edition(TimeStampedModel):
    work = models.ForeignKey(Work, on_delete=models.CASCADE, related_name="editions")
    isbn_10 = models.CharField(max_length=10, unique=True, null=True, blank=True)
    isbn_13 = models.CharField(max_length=13, unique=True, null=True, blank=True)
    publisher = models.CharField(max_length=200, blank=True)
    publication_year = models.PositiveSmallIntegerField(null=True, blank=True)
    language = models.CharField(max_length=16, default="en")
    format = models.CharField(
        max_length=24, choices=EditionFormat.choices, default=EditionFormat.HARDCOVER
    )
    edition_statement = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    cover_image = models.URLField(blank=True)
    material_type = models.ForeignKey(
        "MaterialType", on_delete=models.SET_NULL, null=True, blank=True, related_name="editions"
    )
    public_status = models.CharField(
        max_length=16, choices=PublicStatus.choices, default=PublicStatus.PUBLISHED
    )

    class Meta:
        ordering = ["work__canonical_title", "publication_year", "format"]
        indexes = [
            models.Index(fields=["isbn_13"], name="edition_isbn13_idx"),
            models.Index(fields=["isbn_10"], name="edition_isbn10_idx"),
        ]

    def __str__(self) -> str:
        suffix = f" ({self.publication_year})" if self.publication_year else ""
        return f"{self.work.canonical_title}{suffix}"


class WorkSearchDocument(TimeStampedModel):
    work = models.OneToOneField(Work, on_delete=models.CASCADE, related_name="search_row")
    search_document = models.TextField(blank=True)
    search_vector = SearchVectorField(null=True, blank=True)
    # Deterministic local semantic embedding of the search document (see search.py).
    # Stored as a plain float array so semantic ranking works without pgvector.
    embedding = ArrayField(models.FloatField(), null=True, blank=True)

    class Meta:
        indexes = [GinIndex(fields=["search_vector"], name="work_search_vector_gin")]

    def __str__(self) -> str:
        return f"Search document for {self.work}"


class Collection(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="collections"
    )
    name = models.CharField(max_length=160)
    slug = models.SlugField()
    description = models.TextField(blank=True)
    works = models.ManyToManyField(Work, related_name="collections", blank=True)
    public = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "slug"], name="uniq_collection_slug_org"
            )
        ]

    def __str__(self) -> str:
        return self.name


class CopyStatus(models.TextChoices):
    AVAILABLE = "available", "Available"
    LOANED = "loaned", "Loaned"
    ON_HOLD = "on_hold", "On Hold"
    IN_TRANSIT = "in_transit", "In Transit"
    ILL = "ill", "On Inter-Library Loan"
    LOST = "lost", "Lost"
    RETIRED = "retired", "Retired"
    REPAIR = "repair", "Repair"


class CopyCondition(models.TextChoices):
    NEW = "new", "New"
    GOOD = "good", "Good"
    WORN = "worn", "Worn"
    DAMAGED = "damaged", "Damaged"


class Copy(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="copies")
    edition = models.ForeignKey(Edition, on_delete=models.PROTECT, related_name="copies")
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="copies")
    shelf_location = models.ForeignKey(
        ShelfLocation, on_delete=models.SET_NULL, null=True, blank=True, related_name="copies"
    )
    barcode = models.CharField(max_length=80)
    acquisition_date = models.DateField(null=True, blank=True)
    condition = models.CharField(
        max_length=16, choices=CopyCondition.choices, default=CopyCondition.GOOD
    )
    public_visible = models.BooleanField(default=True)
    status = models.CharField(
        max_length=16, choices=CopyStatus.choices, default=CopyStatus.AVAILABLE
    )
    # Floating item: settles at (is re-homed to) the branch where it is returned,
    # rather than transiting back to an owning branch.
    floating = models.BooleanField(default=False)
    staff_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["branch__name", "barcode"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "barcode"], name="uniq_copy_barcode_org"
            )
        ]
        indexes = [
            models.Index(fields=["edition", "branch", "status"], name="copy_edition_branch_status"),
            models.Index(fields=["organization", "status"], name="copy_org_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.barcode} - {self.edition}"


class LoanStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RETURNED = "returned", "Returned"
    OVERDUE = "overdue", "Overdue"
    LOST = "lost", "Lost"
    CLAIMS_RETURNED = "claims_returned", "Claims returned"


class Loan(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="loans")
    copy = models.ForeignKey(Copy, on_delete=models.PROTECT, related_name="loans")
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="loans"
    )
    patron_hash = models.CharField(max_length=96, blank=True)
    borrowed_at = models.DateTimeField(default=timezone.now)
    due_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=LoanStatus.choices, default=LoanStatus.ACTIVE)
    renewal_count = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["-borrowed_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["copy"],
                condition=Q(status__in=[LoanStatus.ACTIVE, LoanStatus.OVERDUE]),
                name="uniq_active_loan_per_copy",
            ),
            models.CheckConstraint(
                condition=Q(status=LoanStatus.RETURNED, returned_at__isnull=False)
                | ~Q(status=LoanStatus.RETURNED),
                name="returned_loan_has_returned_at",
            ),
        ]
        indexes = [
            models.Index(fields=["patron", "status"], name="loan_patron_status_idx"),
            models.Index(fields=["copy", "status"], name="loan_copy_status_idx"),
            models.Index(fields=["due_at", "status"], name="loan_due_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.copy.barcode} due {self.due_at:%Y-%m-%d}"

    @property
    def work(self):
        return self.copy.edition.work


class HoldStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    READY = "ready", "Ready"
    FULFILLED = "fulfilled", "Fulfilled"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class Hold(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="holds")
    work = models.ForeignKey(Work, on_delete=models.PROTECT, related_name="holds")
    patron = models.ForeignKey(PatronProfile, on_delete=models.CASCADE, related_name="holds")
    preferred_branch = models.ForeignKey(
        Branch, on_delete=models.PROTECT, related_name="preferred_holds"
    )
    assigned_copy = models.ForeignKey(
        Copy, on_delete=models.PROTECT, null=True, blank=True, related_name="assigned_holds"
    )
    loan = models.ForeignKey(
        Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name="fulfilled_holds"
    )
    ready_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=16, choices=HoldStatus.choices, default=HoldStatus.WAITING)
    transit_attempts = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "work", "patron"],
                condition=Q(status__in=[HoldStatus.WAITING, HoldStatus.READY]),
                name="uniq_active_hold_per_patron_work",
            ),
            models.CheckConstraint(
                condition=Q(status=HoldStatus.READY, assigned_copy__isnull=False)
                | ~Q(status=HoldStatus.READY),
                name="ready_hold_has_assigned_copy",
            ),
            models.CheckConstraint(
                condition=Q(status=HoldStatus.FULFILLED, loan__isnull=False)
                | ~Q(status=HoldStatus.FULFILLED),
                name="fulfilled_hold_has_loan",
            ),
        ]
        indexes = [
            models.Index(fields=["work", "status", "created_at"], name="hold_work_status_created"),
            models.Index(fields=["patron", "status"], name="hold_patron_status_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.work} for {self.patron} ({self.status})"


class Renewal(TimeStampedModel):
    loan = models.ForeignKey(Loan, on_delete=models.CASCADE, related_name="renewals")
    renewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    old_due_at = models.DateTimeField()
    new_due_at = models.DateTimeField()
    source = models.CharField(max_length=32, default="web")
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class CopyMovement(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="copy_movements"
    )
    copy = models.ForeignKey(Copy, on_delete=models.CASCADE, related_name="movements")
    from_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="copy_movements_from"
    )
    to_branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="copy_movements_to"
    )
    from_status = models.CharField(max_length=16, blank=True)
    to_status = models.CharField(max_length=16, blank=True)
    reason = models.CharField(max_length=120)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class LibrarianOverride(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="overrides"
    )
    actor = models.ForeignKey(User, on_delete=models.PROTECT, related_name="librarian_overrides")
    reason = models.TextField()
    entity_type = models.CharField(max_length=120)
    entity_id = models.CharField(max_length=120)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]


class NotificationTemplate(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="notification_templates"
    )
    key = models.CharField(max_length=120)
    channel = models.CharField(max_length=32, default="email")
    subject = models.CharField(max_length=200)
    body = models.TextField()
    active = models.BooleanField(default=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["organization", "key", "channel"], name="uniq_template")
        ]


class NotificationDelivery(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="notification_deliveries"
    )
    recipient = models.CharField(max_length=254)
    channel = models.CharField(max_length=32, default="email")
    template_key = models.CharField(max_length=120)
    related_entity = models.CharField(max_length=160, blank=True)
    status = models.CharField(max_length=32, default="pending")
    provider_reference = models.CharField(max_length=160, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    failed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            # One delivery row per originating event (provider_reference holds the
            # domain-event id). Claimed before sending so it acts as the delivery
            # idempotency gate; event-less rows (blank provider_reference) are
            # exempt.
            models.UniqueConstraint(
                fields=["organization", "provider_reference"],
                condition=~Q(provider_reference=""),
                name="uniq_delivery_per_event",
            )
        ]


class SearchQueryLog(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="search_logs"
    )
    query = models.CharField(max_length=500, blank=True)
    filters = models.JSONField(default=dict, blank=True)
    result_count = models.PositiveIntegerField(default=0)
    selected_work = models.ForeignKey(Work, on_delete=models.SET_NULL, null=True, blank=True)
    latency_ms = models.PositiveIntegerField(default=0)
    user_or_session_hash = models.CharField(max_length=96, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["created_at", "result_count"], name="search_log_result_idx"),
            models.Index(fields=["latency_ms"], name="search_log_latency_idx"),
        ]


class DomainEvent(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="domain_events", null=True, blank=True
    )
    event_type = models.CharField(max_length=120)
    aggregate_type = models.CharField(max_length=120)
    aggregate_id = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    source = models.CharField(max_length=64, default="web")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["event_type", "created_at"], name="domain_event_type_time")]


class AuditLog(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="audit_logs", null=True, blank=True
    )
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    source = models.CharField(max_length=64, default="web")
    action = models.CharField(max_length=120)
    entity_type = models.CharField(max_length=120)
    entity_id = models.CharField(max_length=120)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField(blank=True)
    request_id = models.CharField(max_length=120, blank=True)
    ip_hash = models.CharField(max_length=96, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["action", "created_at"], name="audit_action_time")]

    def save(self, *args, **kwargs):
        # Append-only: audit rows are written once and never edited. Deletion is
        # allowed only via the retention job (prune_logs). A DB trigger enforces
        # the no-UPDATE rule at the database level as well (migration 0026).
        if not self._state.adding:
            raise ValueError("AuditLog is append-only and cannot be modified.")
        super().save(*args, **kwargs)


class OutboxStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class OutboxEvent(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="outbox_events", null=True, blank=True
    )
    event_type = models.CharField(max_length=120)
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, choices=OutboxStatus.choices, default=OutboxStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        ordering = ["next_attempt_at", "id"]
        indexes = [models.Index(fields=["status", "next_attempt_at"], name="outbox_status_due_idx")]


class CatalogImportStatus(models.TextChoices):
    STAGED = "staged", "Staged"
    VALIDATED = "validated", "Validated"
    COMMITTED = "committed", "Committed"
    FAILED = "failed", "Failed"
    ROLLED_BACK = "rolled_back", "Rolled Back"


class CatalogImportBatch(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="import_batches"
    )
    source_file = models.FileField(upload_to="imports/", blank=True)
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=CatalogImportStatus.choices, default=CatalogImportStatus.STAGED
    )
    row_count = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    validation_summary = models.JSONField(default=dict, blank=True)
    committed_at = models.DateTimeField(null=True, blank=True)
    rolled_back_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class CatalogImportRow(TimeStampedModel):
    batch = models.ForeignKey(CatalogImportBatch, on_delete=models.CASCADE, related_name="rows")
    row_number = models.PositiveIntegerField()
    row_payload = models.JSONField(default=dict)
    parsed_fields = models.JSONField(default=dict, blank=True)
    validation_errors = models.JSONField(default=list, blank=True)
    matched_existing = models.JSONField(default=dict, blank=True)
    commit_result = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["batch", "row_number"]
        constraints = [
            models.UniqueConstraint(fields=["batch", "row_number"], name="uniq_import_row_number")
        ]


class ScopedApiToken(TimeStampedModel):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(max_length=120)
    prefix = models.CharField(max_length=16, db_index=True)
    key_hash = models.CharField(max_length=256)
    scopes = models.JSONField(default=list, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    @classmethod
    def issue(cls, user, organization, name: str, scopes: list[str]):
        raw_key = secrets.token_urlsafe(32)
        return raw_key, cls.objects.create(
            user=user,
            organization=organization,
            name=name,
            prefix=raw_key[:12],
            key_hash=make_password(raw_key),
            scopes=scopes,
        )

    def verify(self, raw_key: str) -> bool:
        return self.revoked_at is None and check_password(raw_key, self.key_hash)

    def mark_used(self) -> None:
        self.last_used_at = timezone.now()
        self.save(update_fields=["last_used_at", "updated_at"])


class Room(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="rooms")
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="rooms")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=160)
    capacity = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["branch__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_room_code")
        ]

    def __str__(self) -> str:
        return f"{self.name} @ {self.branch.name}"


class ReservationStatus(models.TextChoices):
    BOOKED = "booked", "Booked"
    CANCELLED = "cancelled", "Cancelled"


class RoomReservation(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="room_reservations"
    )
    room = models.ForeignKey(Room, on_delete=models.CASCADE, related_name="reservations")
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.CASCADE, related_name="room_reservations"
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    purpose = models.CharField(max_length=200, blank=True)
    status = models.CharField(
        max_length=16, choices=ReservationStatus.choices, default=ReservationStatus.BOOKED
    )

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["room", "status", "starts_at"], name="resv_room_status")]


class Event(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="events")
    branch = models.ForeignKey(
        Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name="events"
    )
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    capacity = models.PositiveIntegerField(default=0)  # 0 == unlimited
    public = models.BooleanField(default=True)

    class Meta:
        ordering = ["starts_at"]
        indexes = [models.Index(fields=["organization", "starts_at"], name="event_org_start")]

    def __str__(self) -> str:
        return self.title


class RegistrationStatus(models.TextChoices):
    REGISTERED = "registered", "Registered"
    WAITLISTED = "waitlisted", "Waitlisted"
    CANCELLED = "cancelled", "Cancelled"


class EventRegistration(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="event_registrations"
    )
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="registrations")
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.CASCADE, related_name="event_registrations"
    )
    status = models.CharField(
        max_length=16, choices=RegistrationStatus.choices, default=RegistrationStatus.REGISTERED
    )

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "patron"],
                condition=Q(status__in=["registered", "waitlisted"]),
                name="uniq_active_registration",
            )
        ]


class WebhookEndpoint(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="webhook_endpoints"
    )
    url = models.URLField()
    secret = models.CharField(max_length=128, blank=True)
    # Event types to deliver; ["*"] == all.
    event_types = models.JSONField(default=list, blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]

    def matches(self, event_type: str) -> bool:
        types = self.event_types or ["*"]
        return "*" in types or event_type in types

    def __str__(self) -> str:
        return self.url


class WebhookDeliveryStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    DELIVERED = "delivered", "Delivered"
    FAILED = "failed", "Failed"


class WebhookDelivery(TimeStampedModel):
    endpoint = models.ForeignKey(
        WebhookEndpoint, on_delete=models.CASCADE, related_name="deliveries"
    )
    event_type = models.CharField(max_length=120)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=16, choices=WebhookDeliveryStatus.choices, default=WebhookDeliveryStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    response_status = models.PositiveSmallIntegerField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    next_attempt_at = models.DateTimeField(default=timezone.now)
    delivered_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["next_attempt_at", "id"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="webhook_status_due_idx")
        ]


class SsoConnection(TimeStampedModel):
    """Per-tenant OpenID Connect configuration."""

    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="sso_connection"
    )
    provider = models.CharField(max_length=32, default="oidc")
    client_id = models.CharField(max_length=200)
    client_secret = models.CharField(max_length=255, blank=True)
    authorize_url = models.URLField()
    token_url = models.URLField()
    userinfo_url = models.URLField()
    enabled = models.BooleanField(default=True)

    def __str__(self) -> str:
        return f"SSO for {self.organization}"


class SsoIdentity(TimeStampedModel):
    connection = models.ForeignKey(
        SsoConnection, on_delete=models.CASCADE, related_name="identities"
    )
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="sso_identities")
    subject = models.CharField(max_length=255)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["connection", "subject"], name="uniq_sso_subject")
        ]


class Review(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="reviews")
    work = models.ForeignKey(Work, on_delete=models.CASCADE, related_name="reviews")
    patron = models.ForeignKey(PatronProfile, on_delete=models.CASCADE, related_name="reviews")
    rating = models.PositiveSmallIntegerField()
    body = models.TextField(blank=True)
    public = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["patron", "work"], name="uniq_review_per_patron_work"),
            models.CheckConstraint(
                condition=Q(rating__gte=1) & Q(rating__lte=5), name="review_rating_range"
            ),
        ]
        indexes = [models.Index(fields=["work", "public"], name="review_work_public_idx")]


class ReadingList(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="reading_lists"
    )
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.CASCADE, related_name="reading_lists"
    )
    name = models.CharField(max_length=160)
    public = models.BooleanField(default=False)
    works = models.ManyToManyField(Work, related_name="reading_lists", blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Vendor(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="vendors")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=200)
    contact_email = models.EmailField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_vendor_code")
        ]

    def __str__(self) -> str:
        return self.name


class Fund(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="funds")
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=200)
    budget_cents = models.PositiveIntegerField(default=0)
    spent_cents = models.PositiveIntegerField(default=0)
    # Committed-but-not-yet-spent funds (open purchase orders).
    encumbered_cents = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_fund_code")
        ]

    @property
    def remaining_cents(self) -> int:
        return max(0, self.budget_cents - self.spent_cents)

    @property
    def available_cents(self) -> int:
        """Budget still free to commit: neither spent nor encumbered."""
        return max(0, self.budget_cents - self.spent_cents - self.encumbered_cents)

    def __str__(self) -> str:
        return f"{self.name} ({self.remaining_cents}c left)"


class PurchaseOrderStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    ORDERED = "ordered", "Ordered"
    RECEIVED = "received", "Received"
    CANCELLED = "cancelled", "Cancelled"


class PurchaseOrder(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="purchase_orders"
    )
    vendor = models.ForeignKey(Vendor, on_delete=models.PROTECT, related_name="purchase_orders")
    fund = models.ForeignKey(Fund, on_delete=models.PROTECT, related_name="purchase_orders")
    status = models.CharField(
        max_length=16, choices=PurchaseOrderStatus.choices, default=PurchaseOrderStatus.DRAFT
    )
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    ordered_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"PO #{self.pk} — {self.vendor} ({self.status})"


class PurchaseOrderLine(TimeStampedModel):
    purchase_order = models.ForeignKey(
        PurchaseOrder, on_delete=models.CASCADE, related_name="lines"
    )
    edition = models.ForeignKey(
        Edition, on_delete=models.PROTECT, null=True, blank=True, related_name="order_lines"
    )
    title_text = models.CharField(max_length=300, blank=True)
    branch = models.ForeignKey(Branch, on_delete=models.PROTECT, related_name="order_lines")
    quantity = models.PositiveIntegerField(default=1)
    unit_cost_cents = models.PositiveIntegerField(default=0)
    received_quantity = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["id"]

    @property
    def outstanding(self) -> int:
        return max(0, self.quantity - self.received_quantity)


class LicenseModel(models.TextChoices):
    ONE_COPY_ONE_USER = "ocou", "One copy / one user"
    METERED_CHECKOUTS = "metered_checkouts", "Metered (checkouts)"
    METERED_TIME = "metered_time", "Metered (time)"
    SIMULTANEOUS = "simultaneous", "Simultaneous use"


class DigitalLicense(TimeStampedModel):
    """A licensed digital title (ebook/audiobook). Availability derives from the
    license model rather than physical copies."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="digital_licenses"
    )
    edition = models.ForeignKey(Edition, on_delete=models.CASCADE, related_name="digital_licenses")
    license_model = models.CharField(
        max_length=24, choices=LicenseModel.choices, default=LicenseModel.ONE_COPY_ONE_USER
    )
    # Max simultaneous active loans; null == unlimited (simultaneous-use).
    concurrent_limit = models.PositiveIntegerField(null=True, blank=True, default=1)
    checkouts_allowed = models.PositiveIntegerField(null=True, blank=True)  # metered_checkouts
    checkouts_used = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(null=True, blank=True)  # metered_time
    loan_period_days = models.PositiveSmallIntegerField(default=21)
    content_url = models.URLField(blank=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["edition", "active"], name="diglicense_edition_active")]

    def __str__(self) -> str:
        return f"{self.edition} ({self.get_license_model_display()})"


class DigitalLoanStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    RETURNED = "returned", "Returned"
    EXPIRED = "expired", "Expired"


class DigitalLoan(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="digital_loans"
    )
    license = models.ForeignKey(
        DigitalLicense, on_delete=models.PROTECT, related_name="loans"
    )
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="digital_loans"
    )
    patron_hash = models.CharField(max_length=96, blank=True)
    started_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    returned_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=16, choices=DigitalLoanStatus.choices, default=DigitalLoanStatus.ACTIVE
    )
    access_token = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["license", "status"], name="digloan_license_status"),
            models.Index(fields=["status", "expires_at"], name="digloan_status_expiry"),
            models.Index(fields=["patron", "status"], name="digloan_patron_status"),
        ]

    @property
    def edition(self):
        return self.license.edition


class DigitalHoldStatus(models.TextChoices):
    WAITING = "waiting", "Waiting"
    READY = "ready", "Ready"
    FULFILLED = "fulfilled", "Fulfilled"
    EXPIRED = "expired", "Expired"
    CANCELLED = "cancelled", "Cancelled"


class DigitalHold(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="digital_holds"
    )
    edition = models.ForeignKey(Edition, on_delete=models.CASCADE, related_name="digital_holds")
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.CASCADE, related_name="digital_holds"
    )
    status = models.CharField(
        max_length=16, choices=DigitalHoldStatus.choices, default=DigitalHoldStatus.WAITING
    )
    ready_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [
            models.Index(fields=["edition", "status", "created_at"], name="dighold_edition_status")
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "edition", "patron"],
                condition=Q(status__in=["waiting", "ready"]),
                name="uniq_active_digital_hold",
            )
        ]


class StoredBlob(TimeStampedModel):
    """Durable, DB-backed binary content store — keeps the reader fully offline/testable.

    In production a storage-backed store can replace this; the delivery layer
    reads/writes blobs through a small interface either way.
    """

    key = models.CharField(max_length=128, unique=True)
    content_type = models.CharField(max_length=100, default="application/octet-stream")
    byte_size = models.PositiveIntegerField(default=0)
    data = models.BinaryField()

    def __str__(self) -> str:
        return f"{self.key} ({self.byte_size} bytes)"


class DigitalAssetFormat(models.TextChoices):
    TEXT = "text", "Text (chaptered)"
    EPUB = "epub", "EPUB"
    PDF = "pdf", "PDF"
    AUDIO = "audio", "Audiobook"


class DigitalAsset(TimeStampedModel):
    """The deliverable content for an edition: chaptered text or a binary blob."""

    edition = models.ForeignKey(Edition, on_delete=models.CASCADE, related_name="digital_assets")
    fmt = models.CharField(
        max_length=16, choices=DigitalAssetFormat.choices, default=DigitalAssetFormat.TEXT
    )
    title = models.CharField(max_length=255, blank=True)
    # For chaptered text: [{"title": "...", "body": "..."}, ...]
    text_content = models.JSONField(default=list, blank=True)
    # For binary formats: the StoredBlob key + duration for audio.
    media_key = models.CharField(max_length=128, blank=True)
    content_type = models.CharField(max_length=100, blank=True)
    byte_size = models.PositiveIntegerField(default=0)
    duration_seconds = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["edition", "fmt"]
        constraints = [
            models.UniqueConstraint(fields=["edition", "fmt"], name="uniq_asset_edition_fmt")
        ]

    def __str__(self) -> str:
        return f"{self.title or self.edition} [{self.fmt}]"


class ReadingProgress(TimeStampedModel):
    """A patron's last read/listen position within a digital loan (bookmark sync)."""

    loan = models.OneToOneField(
        DigitalLoan, on_delete=models.CASCADE, related_name="reading_progress"
    )
    locator = models.CharField(max_length=255, blank=True)  # e.g. "chapter:3" or "t:812.4"
    percent = models.FloatField(default=0.0)

    def __str__(self) -> str:
        return f"{self.loan_id} @ {self.locator or self.percent}"


class DigitalAccessLog(TimeStampedModel):
    """Traceable record of content access for DRM/audit. Holds no raw patron PII."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="digital_access_logs"
    )
    loan = models.ForeignKey(
        DigitalLoan, on_delete=models.SET_NULL, null=True, blank=True, related_name="access_logs"
    )
    patron_hash = models.CharField(max_length=96, blank=True)
    action = models.CharField(max_length=32, default="open")
    detail = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["loan", "action"], name="dal_loan_action")]


class PatronType(TimeStampedModel):
    """A patron category (Adult, Child, Staff…) with its circulation allowances."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="patron_types"
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=120)
    max_loans = models.PositiveSmallIntegerField(default=12)
    max_holds = models.PositiveSmallIntegerField(default=8)

    class Meta:
        ordering = ["organization__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_patron_type_code")
        ]

    def __str__(self) -> str:
        return self.name


class MaterialType(TimeStampedModel):
    """A material category (Book, DVD, Reference, Equipment…)."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="material_types"
    )
    code = models.CharField(max_length=32)
    name = models.CharField(max_length=120)

    class Meta:
        ordering = ["organization__name", "name"]
        constraints = [
            models.UniqueConstraint(fields=["organization", "code"], name="uniq_material_type_code")
        ]

    def __str__(self) -> str:
        return self.name


class CirculationPolicy(TimeStampedModel):
    """One cell of the (patron_type × material_type) circulation matrix.

    A null patron_type or material_type acts as a wildcard, so a single row can
    be a per-type default or a global default (both null).
    """

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="circulation_policies"
    )
    patron_type = models.ForeignKey(
        PatronType, on_delete=models.CASCADE, null=True, blank=True, related_name="policies"
    )
    material_type = models.ForeignKey(
        MaterialType, on_delete=models.CASCADE, null=True, blank=True, related_name="policies"
    )
    loan_days = models.PositiveSmallIntegerField(default=21)
    max_renewals = models.PositiveSmallIntegerField(default=2)
    hold_pickup_days = models.PositiveSmallIntegerField(default=7)
    holdable = models.BooleanField(default=True)
    # Null => inherit the organization's FeePolicy rate.
    daily_overdue_cents = models.PositiveIntegerField(null=True, blank=True)
    max_overdue_cents = models.PositiveIntegerField(null=True, blank=True)

    class Meta:
        ordering = ["organization__name", "patron_type__name", "material_type__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "patron_type", "material_type"],
                name="uniq_circulation_policy_cell",
            )
        ]

    def __str__(self) -> str:
        pt = self.patron_type.name if self.patron_type else "any patron"
        mt = self.material_type.name if self.material_type else "any material"
        return f"{pt} × {mt}"


class Plan(TimeStampedModel):
    """A subscription tier. Null limit == unlimited."""

    slug = models.SlugField(unique=True)
    name = models.CharField(max_length=120)
    price_cents = models.PositiveIntegerField(default=0)
    billing_interval = models.CharField(max_length=16, default="month")
    max_branches = models.PositiveIntegerField(null=True, blank=True)
    max_patrons = models.PositiveIntegerField(null=True, blank=True)
    max_copies = models.PositiveIntegerField(null=True, blank=True)
    features = models.JSONField(default=list, blank=True)
    public = models.BooleanField(default=True)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["price_cents", "name"]

    def __str__(self) -> str:
        return self.name


class SubscriptionStatus(models.TextChoices):
    TRIALING = "trialing", "Trialing"
    ACTIVE = "active", "Active"
    PAST_DUE = "past_due", "Past Due"
    CANCELED = "canceled", "Canceled"


class Subscription(TimeStampedModel):
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="subscription"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="subscriptions")
    status = models.CharField(
        max_length=16, choices=SubscriptionStatus.choices, default=SubscriptionStatus.TRIALING
    )
    trial_ends_at = models.DateTimeField(null=True, blank=True)
    current_period_end = models.DateTimeField(null=True, blank=True)
    external_customer_id = models.CharField(max_length=120, blank=True)
    external_subscription_id = models.CharField(max_length=120, blank=True)
    dunning_attempts = models.PositiveSmallIntegerField(default=0)
    grace_until = models.DateTimeField(null=True, blank=True)
    # Carry-forward account credit (e.g. from a mid-period downgrade), applied to
    # the next charge before hitting the card.
    credit_cents = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["organization__name"]

    def __str__(self) -> str:
        return f"{self.organization} — {self.plan} ({self.status})"

    @property
    def is_serviceable(self) -> bool:
        return self.status in {SubscriptionStatus.TRIALING, SubscriptionStatus.ACTIVE}


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    OPEN = "open", "Open"
    PAID = "paid", "Paid"
    VOID = "void", "Void"
    UNCOLLECTIBLE = "uncollectible", "Uncollectible"


class Invoice(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="invoices"
    )
    subscription = models.ForeignKey(
        Subscription, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices"
    )
    amount_cents = models.PositiveIntegerField(default=0)
    currency = models.CharField(max_length=8, default="usd")
    status = models.CharField(
        max_length=16, choices=InvoiceStatus.choices, default=InvoiceStatus.OPEN
    )
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    external_invoice_id = models.CharField(max_length=120, blank=True)
    description = models.CharField(max_length=200, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Invoice ${self.amount_cents / 100:.2f} ({self.status})"


class InvoiceLineItem(TimeStampedModel):
    """A single charge or credit on an invoice. Amount may be negative (proration credit)."""

    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="line_items")
    description = models.CharField(max_length=200)
    amount_cents = models.IntegerField(default=0)

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.description}: ${self.amount_cents / 100:.2f}"


class PaymentMethod(TimeStampedModel):
    """A simulated card on file. No real PAN is ever stored — brand + last4 only."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="payment_methods"
    )
    gateway_ref = models.CharField(max_length=120, blank=True)
    brand = models.CharField(max_length=32, default="visa")
    last4 = models.CharField(max_length=4, default="4242")
    exp_month = models.PositiveSmallIntegerField(default=12)
    exp_year = models.PositiveSmallIntegerField(default=2030)
    is_default = models.BooleanField(default=True)

    class Meta:
        ordering = ["-is_default", "-created_at"]

    def __str__(self) -> str:
        return f"{self.brand} ****{self.last4}"


class CheckoutStatus(models.TextChoices):
    OPEN = "open", "Open"
    COMPLETED = "completed", "Completed"
    EXPIRED = "expired", "Expired"


class CheckoutSession(TimeStampedModel):
    """A simulated hosted-checkout session, mirroring the Stripe Checkout flow."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="checkout_sessions"
    )
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT, related_name="checkout_sessions")
    token = models.CharField(max_length=64, unique=True)
    status = models.CharField(
        max_length=16, choices=CheckoutStatus.choices, default=CheckoutStatus.OPEN
    )
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Checkout {self.token[:8]} ({self.status})"


class FeePolicy(TimeStampedModel):
    organization = models.OneToOneField(
        Organization, on_delete=models.CASCADE, related_name="fee_policy"
    )
    daily_overdue_cents = models.PositiveIntegerField(default=25)
    grace_days = models.PositiveSmallIntegerField(default=0)
    max_overdue_cents = models.PositiveIntegerField(default=2000)
    lost_item_fee_cents = models.PositiveIntegerField(default=3000)
    # Patrons owing more than this are blocked from borrowing (0 == never block).
    block_threshold_cents = models.PositiveIntegerField(default=1000)

    def __str__(self) -> str:
        return f"Fee policy for {self.organization}"


class FeeType(models.TextChoices):
    OVERDUE = "overdue", "Overdue"
    LOST = "lost", "Lost item"
    DAMAGED = "damaged", "Damaged item"
    PROCESSING = "processing", "Processing"
    MANUAL = "manual", "Manual"


class FeeStatus(models.TextChoices):
    OUTSTANDING = "outstanding", "Outstanding"
    PAID = "paid", "Paid"
    WAIVED = "waived", "Waived"


class Fee(TimeStampedModel):
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE, related_name="fees")
    patron = models.ForeignKey(PatronProfile, on_delete=models.CASCADE, related_name="fees")
    loan = models.ForeignKey(
        Loan, on_delete=models.SET_NULL, null=True, blank=True, related_name="fees"
    )
    fee_type = models.CharField(max_length=16, choices=FeeType.choices)
    amount_cents = models.PositiveIntegerField(default=0)
    paid_cents = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=FeeStatus.choices, default=FeeStatus.OUTSTANDING
    )
    description = models.CharField(max_length=200, blank=True)
    waived_reason = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["patron", "status"], name="fee_patron_status_idx")]
        constraints = [
            # At most one accruing overdue fee per loan (updated as it accrues).
            models.UniqueConstraint(
                fields=["loan"],
                condition=Q(fee_type="overdue"),
                name="uniq_overdue_fee_per_loan",
            )
        ]

    @property
    def balance_cents(self) -> int:
        if self.status == FeeStatus.WAIVED:
            return 0
        return max(0, self.amount_cents - self.paid_cents)

    def __str__(self) -> str:
        return f"{self.get_fee_type_display()} ${self.amount_cents / 100:.2f} ({self.status})"


class Payment(TimeStampedModel):
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="payments"
    )
    patron = models.ForeignKey(PatronProfile, on_delete=models.CASCADE, related_name="payments")
    amount_cents = models.PositiveIntegerField()
    method = models.CharField(max_length=32, default="online")
    # "payment" moves money in; "refund" reverses a prior payment.
    kind = models.CharField(max_length=16, default="payment")
    reference = models.CharField(max_length=160, blank=True)
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        sign = "-" if self.kind == "refund" else ""
        return f"{sign}${self.amount_cents / 100:.2f} via {self.method}"


class PaymentAllocation(TimeStampedModel):
    """How much of a specific payment was applied to a specific fee.

    Lets a refund reverse exactly the fees *that* payment paid, instead of
    guessing newest-first.
    """

    payment = models.ForeignKey(Payment, on_delete=models.CASCADE, related_name="allocations")
    fee = models.ForeignKey(Fee, on_delete=models.CASCADE, related_name="allocations")
    amount_cents = models.PositiveIntegerField()
    reversed_cents = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["pk"]

    @property
    def remaining_cents(self) -> int:
        return max(0, self.amount_cents - self.reversed_cents)


def stable_patron_hash(patron: PatronProfile | None) -> str:
    if patron is None:
        return ""
    material = (
        f"{settings.SECRET_KEY}:{patron.organization_id}:{patron.pk}:{patron.library_card_number}"
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Consortia & resource sharing
# --------------------------------------------------------------------------- #
class Consortium(TimeStampedModel):
    """A group of independent libraries that share resources (ILL, floating)."""

    name = models.CharField(max_length=200)
    slug = models.SlugField(unique=True)
    allow_ill = models.BooleanField(default=True)
    allow_floating = models.BooleanField(default=False)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ConsortiumMembership(TimeStampedModel):
    consortium = models.ForeignKey(
        Consortium, on_delete=models.CASCADE, related_name="memberships"
    )
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="consortium_memberships"
    )
    lends = models.BooleanField(default=True)
    borrows = models.BooleanField(default=True)

    class Meta:
        ordering = ["organization__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["consortium", "organization"], name="uniq_consortium_member"
            )
        ]

    def __str__(self) -> str:
        return f"{self.organization} in {self.consortium}"


class IllStatus(models.TextChoices):
    UNFILLED = "unfilled", "Unfilled"
    REQUESTED = "requested", "Requested"
    SHIPPED = "shipped", "Shipped"
    ON_LOAN = "on_loan", "On loan"
    RETURNING = "returning", "Returning"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class IllRequest(TimeStampedModel):
    """An inter-library loan: one member borrows a physical copy from another.

    The borrowed copy stays owned by the lender; the whole loan lifecycle is
    tracked here rather than as a cross-tenant Loan row.
    """

    consortium = models.ForeignKey(
        Consortium, on_delete=models.PROTECT, related_name="ill_requests"
    )
    work = models.ForeignKey(Work, on_delete=models.PROTECT, related_name="ill_requests")
    requesting_org = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="ill_requests_out"
    )
    requesting_patron = models.ForeignKey(
        PatronProfile, on_delete=models.SET_NULL, null=True, blank=True, related_name="ill_requests"
    )
    patron_hash = models.CharField(max_length=96, blank=True)
    lending_org = models.ForeignKey(
        Organization, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="ill_requests_in",
    )
    lending_copy = models.ForeignKey(
        Copy, on_delete=models.SET_NULL, null=True, blank=True, related_name="ill_requests"
    )
    status = models.CharField(
        max_length=16, choices=IllStatus.choices, default=IllStatus.REQUESTED
    )
    shipped_at = models.DateTimeField(null=True, blank=True)
    borrowed_at = models.DateTimeField(null=True, blank=True)
    due_at = models.DateTimeField(null=True, blank=True)
    returned_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["requesting_org", "status"], name="ill_reqorg_status"),
            models.Index(fields=["lending_org", "status"], name="ill_lendorg_status"),
            models.Index(fields=["lending_copy", "status"], name="ill_copy_status"),
        ]

    def __str__(self) -> str:
        return f"ILL #{self.pk}: {self.work} ({self.status})"


# --------------------------------------------------------------------------- #
# Financial depth: payment plans
# --------------------------------------------------------------------------- #
class PaymentPlanStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    COMPLETED = "completed", "Completed"
    CANCELLED = "cancelled", "Cancelled"


class PaymentPlan(TimeStampedModel):
    """An arrangement to pay down fees in fixed installments."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="payment_plans"
    )
    patron = models.ForeignKey(
        PatronProfile, on_delete=models.CASCADE, related_name="payment_plans"
    )
    total_cents = models.PositiveIntegerField()
    installment_cents = models.PositiveIntegerField()
    paid_cents = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=16, choices=PaymentPlanStatus.choices, default=PaymentPlanStatus.ACTIVE
    )

    class Meta:
        ordering = ["-created_at"]

    @property
    def remaining_cents(self) -> int:
        return max(0, self.total_cents - self.paid_cents)

    def __str__(self) -> str:
        return f"Plan {self.paid_cents}/{self.total_cents}c ({self.status})"


# --------------------------------------------------------------------------- #
# Enterprise trust: staff MFA (TOTP)
# --------------------------------------------------------------------------- #
class StaffTotpDevice(TimeStampedModel):
    """A TOTP authenticator secret for a staff user (RFC 6238)."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="totp_device")
    # Stored encrypted at rest (see library.mfa); never the raw base32 secret.
    secret = models.CharField(max_length=255)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)

    @property
    def confirmed(self) -> bool:
        return self.confirmed_at is not None

    def __str__(self) -> str:
        state = "confirmed" if self.confirmed else "pending"
        return f"TOTP for {self.user} ({state})"


# --------------------------------------------------------------------------- #
# Staff workflows: inventory / stocktake
# --------------------------------------------------------------------------- #
class InventoryStatus(models.TextChoices):
    OPEN = "open", "Open"
    CLOSED = "closed", "Closed"


class FeatureFlag(TimeStampedModel):
    """A safe-rollout switch. A row with a null organization is the global
    default; a row scoped to an organization overrides it for that tenant."""

    key = models.CharField(max_length=100)
    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, null=True, blank=True, related_name="feature_flags"
    )
    enabled = models.BooleanField(default=False)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["key", "organization__name"]
        constraints = [
            models.UniqueConstraint(
                fields=["key"],
                condition=Q(organization__isnull=True),
                name="uniq_global_feature_flag",
            ),
            models.UniqueConstraint(
                fields=["key", "organization"],
                condition=Q(organization__isnull=False),
                name="uniq_org_feature_flag",
            ),
        ]

    def __str__(self) -> str:
        scope = self.organization.slug if self.organization_id else "global"
        return f"{self.key}={self.enabled} ({scope})"


class InventorySession(TimeStampedModel):
    """A barcode-driven stocktake of a branch's shelves."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="inventory_sessions"
    )
    branch = models.ForeignKey(
        Branch, on_delete=models.CASCADE, related_name="inventory_sessions"
    )
    status = models.CharField(
        max_length=16, choices=InventoryStatus.choices, default=InventoryStatus.OPEN
    )
    started_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True)
    scanned_barcodes = models.JSONField(default=list, blank=True)
    missing_barcodes = models.JSONField(default=list, blank=True)
    unexpected_barcodes = models.JSONField(default=list, blank=True)
    # Scanned items that are actually checked out / in transit — found on the
    # shelf but never checked in; staff must resolve them.
    found_checked_out_barcodes = models.JSONField(default=list, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Inventory {self.branch} ({self.status})"
