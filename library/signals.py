"""Django signals that keep search documents in sync with catalog changes."""
import contextlib
import threading

from django.db.models.signals import m2m_changed, post_save
from django.dispatch import receiver

from .models import Author, Edition, Subject, Work
from .services import rebuild_work_search_document, reindex_author_works

# Fields whose changes affect a Work's denormalized search document. A save that
# only touches other fields (via update_fields) can skip the reindex fan-out.
AUTHOR_SEARCH_FIELDS = {"name", "normalized_name", "sort_name", "aliases"}
SUBJECT_SEARCH_FIELDS = {"name", "public", "slug"}

# Bulk operations (e.g. catalog import) that reindex explicitly can suspend the
# per-object signal reindex to avoid rebuilding the same work many times over.
_suspend_state = threading.local()


def _reindex_suspended() -> bool:
    return getattr(_suspend_state, "active", False)


@contextlib.contextmanager
def suspend_search_reindex():
    previous = getattr(_suspend_state, "active", False)
    _suspend_state.active = True
    try:
        yield
    finally:
        _suspend_state.active = previous


def _touches(update_fields, relevant: set) -> bool:
    # update_fields is None when the caller did not scope the save -> assume the
    # search document may be affected and reindex.
    return update_fields is None or bool(set(update_fields) & relevant)


@receiver(post_save, sender=Work)
def work_saved(sender, instance, **kwargs):
    if instance.pk and not _reindex_suspended():
        rebuild_work_search_document(instance.pk)


@receiver(post_save, sender=Edition)
def edition_saved(sender, instance, **kwargs):
    if not _reindex_suspended():
        rebuild_work_search_document(instance.work_id)


@receiver(m2m_changed, sender=Work.authors.through)
@receiver(m2m_changed, sender=Work.subjects.through)
def work_m2m_changed(sender, instance, action, **kwargs):
    if action in {"post_add", "post_remove", "post_clear"} and not _reindex_suspended():
        rebuild_work_search_document(instance.pk)


@receiver(post_save, sender=Author)
def author_saved(sender, instance, update_fields=None, **kwargs):
    if not _reindex_suspended() and _touches(update_fields, AUTHOR_SEARCH_FIELDS):
        reindex_author_works(instance.pk)


@receiver(post_save, sender=Subject)
def subject_saved(sender, instance, update_fields=None, **kwargs):
    if _reindex_suspended() or not _touches(update_fields, SUBJECT_SEARCH_FIELDS):
        return
    for work_id in instance.works.values_list("id", flat=True):
        rebuild_work_search_document(work_id)
