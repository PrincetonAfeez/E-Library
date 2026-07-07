from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Hold, Loan, Work
from .selectors import availability_for_work


class AuthorMiniSerializer(serializers.Serializer):
    name = serializers.CharField()


class SubjectMiniSerializer(serializers.Serializer):
    name = serializers.CharField()
    slug = serializers.CharField()


class WorkListSerializer(serializers.ModelSerializer):
    authors = AuthorMiniSerializer(many=True)
    subjects = SubjectMiniSerializer(many=True)
    availability = serializers.SerializerMethodField()

    class Meta:
        model = Work
        fields = [
            "id",
            "canonical_title",
            "subtitle",
            "slug",
            "summary",
            "authors",
            "subjects",
            "availability",
        ]

    @extend_schema_field(serializers.DictField)
    def get_availability(self, obj):
        # Prefer a pre-computed page-level map (one grouped query) to avoid a
        # per-work N+1; fall back to a single-work lookup for detail views.
        availability_map = self.context.get("availability_map")
        if availability_map is not None:
            return availability_map.get(
                obj.id, {"available": 0, "loaned": 0, "on_hold": 0, "total": 0}
            )
        return availability_for_work(self.context["organization"], obj)


class LoanSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source="copy.edition.work.canonical_title", read_only=True)
    barcode = serializers.CharField(source="copy.barcode", read_only=True)
    branch = serializers.CharField(source="copy.branch.name", read_only=True)

    class Meta:
        model = Loan
        fields = [
            "id",
            "title",
            "barcode",
            "branch",
            "borrowed_at",
            "due_at",
            "returned_at",
            "status",
            "renewal_count",
        ]


class HoldSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source="work.canonical_title", read_only=True)
    branch = serializers.CharField(source="preferred_branch.name", read_only=True)
    assigned_barcode = serializers.CharField(source="assigned_copy.barcode", read_only=True)

    class Meta:
        model = Hold
        fields = [
            "id",
            "title",
            "branch",
            "assigned_barcode",
            "ready_at",
            "expires_at",
            "status",
            "created_at",
        ]
