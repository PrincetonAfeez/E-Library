"""Django admin registrations for library models."""
from django.contrib import admin

from . import models


@admin.register(models.Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "active", "default_timezone")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(models.Branch)
class BranchAdmin(admin.ModelAdmin):
    list_display = ("name", "organization", "active", "effective_timezone")
    list_filter = ("organization", "active")
    prepopulated_fields = {"slug": ("name",)}


@admin.register(models.Work)
class WorkAdmin(admin.ModelAdmin):
    list_display = ("canonical_title", "public_status", "updated_at")
    list_filter = ("public_status", "subjects")
    search_fields = ("canonical_title", "subtitle", "summary", "authors__name")
    prepopulated_fields = {"slug": ("canonical_title",)}
    filter_horizontal = ("authors", "subjects")


@admin.register(models.Edition)
class EditionAdmin(admin.ModelAdmin):
    list_display = ("work", "format", "publication_year", "publisher", "public_status")
    list_filter = ("format", "language", "public_status")
    search_fields = ("work__canonical_title", "isbn_13", "isbn_10", "publisher")


@admin.register(models.Copy)
class CopyAdmin(admin.ModelAdmin):
    list_display = ("barcode", "edition", "organization", "branch", "status", "public_visible")
    list_filter = ("organization", "branch", "status", "condition")
    search_fields = ("barcode", "edition__work__canonical_title")


admin.site.register(models.Author)
admin.site.register(models.Subject)
admin.site.register(models.ShelfLocation)
admin.site.register(models.PatronProfile)
admin.site.register(models.StaffMembership)
admin.site.register(models.Collection)
admin.site.register(models.WorkSearchDocument)
admin.site.register(models.Loan)
admin.site.register(models.Hold)
admin.site.register(models.Renewal)
admin.site.register(models.CopyMovement)
admin.site.register(models.LibrarianOverride)
admin.site.register(models.NotificationTemplate)
admin.site.register(models.NotificationDelivery)
admin.site.register(models.SearchQueryLog)
admin.site.register(models.DomainEvent)
@admin.register(models.AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Audit logs are append-only: viewable in the admin, never editable."""

    list_display = ("created_at", "action", "entity_type", "entity_id", "actor", "source")
    search_fields = ("action", "entity_type", "entity_id")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
admin.site.register(models.OutboxEvent)
admin.site.register(models.CatalogImportBatch)
admin.site.register(models.CatalogImportRow)
admin.site.register(models.ScopedApiToken)
admin.site.register(models.Room)
admin.site.register(models.RoomReservation)
admin.site.register(models.Event)
admin.site.register(models.EventRegistration)
admin.site.register(models.WebhookEndpoint)
admin.site.register(models.WebhookDelivery)
admin.site.register(models.SsoConnection)
admin.site.register(models.Review)
admin.site.register(models.ReadingList)
admin.site.register(models.Vendor)
admin.site.register(models.Fund)
admin.site.register(models.PurchaseOrder)
admin.site.register(models.PurchaseOrderLine)
admin.site.register(models.DigitalLicense)
admin.site.register(models.DigitalLoan)
admin.site.register(models.DigitalHold)
admin.site.register(models.PatronType)
admin.site.register(models.MaterialType)
admin.site.register(models.CirculationPolicy)
admin.site.register(models.Plan)
admin.site.register(models.Subscription)
admin.site.register(models.Invoice)
admin.site.register(models.PaymentMethod)
admin.site.register(models.CheckoutSession)
admin.site.register(models.FeePolicy)
admin.site.register(models.Fee)
admin.site.register(models.Payment)
admin.site.register(models.DigitalAsset)
admin.site.register(models.StoredBlob)
admin.site.register(models.DigitalAccessLog)
admin.site.register(models.Consortium)
admin.site.register(models.ConsortiumMembership)
admin.site.register(models.IllRequest)
admin.site.register(models.PaymentPlan)
admin.site.register(models.PaymentAllocation)
admin.site.register(models.FeatureFlag)
admin.site.register(models.StaffTotpDevice)
admin.site.register(models.InventorySession)
