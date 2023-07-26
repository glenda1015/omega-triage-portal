import json
import logging
import os
from base64 import b64encode

from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import cache_page
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods
from packageurl import PackageURL

from triage.models import Case, File, Finding, ProjectVersion, WorkItemState
from triage.util.content_managers.file_manager import FileManager
from triage.util.finding_importers.archive_importer import ArchiveImporter
from triage.util.finding_importers.sarif_importer import SARIFImporter
from triage.util.general import clamp
from triage.util.search_parser import parse_query_to_Q
from triage.util.source_viewer import path_to_graph

logger = logging.getLogger(__name__)


@login_required
def show_findings(request: HttpRequest) -> HttpResponse:
    """Shows findings based on a query.

    Params:
        q: query to search for, or all findings if not provided
    """
    query = request.GET.get("q", "").strip()
    page_size = clamp(request.GET.get("page_size", 20), 10, 500)
    page = clamp(request.GET.get("page", 1), 1, 1000)

    findings = Finding.active_findings.all()

    if query:
        findings = Finding.objects.exclude(state=WorkItemState.DELETED)
        query_object = parse_query_to_Q(Finding, query)
        if query_object:
            findings = findings.filter(query_object)

    findings = findings.select_related("project_version", "tool", "file")
    findings = findings.order_by("-project_version__package_url", "title", "created_at")
    paginator = Paginator(findings, page_size)
    page_object = paginator.get_page(page)

    query_string = request.GET.copy()
    if "page" in query_string:
        query_string.pop("page", None)

    context = {
        "query": query,
        "findings": page_object,
        "params": query_string.urlencode(),
    }

    return render(request, "triage/findings_list.html", context)


@login_required
@require_http_methods(["GET", "POST"])
def show_upload(request: HttpRequest) -> HttpResponse:
    """Show the upload form for findings (SARIF, etc.)"""
    if request.method == "GET":
        return render(request, "triage/findings_upload.html")

    if request.method == "POST":
        package_url = PackageURL.from_string(request.POST.get("package_url"))
        files = request.FILES.getlist("file[]")

        if not files:
            return HttpResponseBadRequest("No files provided")

        if request.user.is_anonymous:
            user = get_user_model().objects.get(id=1)
        else:
            user = request.user

        project_version = ProjectVersion.get_or_create_from_package_url(
            package_url,
            user,
        )

        # Find the source code for this project version
        # Get the source based on the package url
        project_version.download_source_code()

        errors = []
        for file in files:
            try:
                archive_importer = ArchiveImporter()
                try:
                    archive_importer.import_archive(
                        file.name,
                        file.read(),
                        project_version,
                        user,
                    )
                except Exception as msg:  # pylint: disable=bare-except
                    print(msg)
                    errors.append("Failed to import archive: " + file.name)

            except:  # pylint: disable=bare-except
                logger.warning("Failed to import SARIF file", exc_info=True)

        # Check if there are any errors and change status based on it
        if not errors:
            status = "ok"
        else:
            status = "error"

        return render(
            request,
            "triage/findings_upload.html",
            {"errors": errors, "status": status},
        )


@login_required
def show_finding_by_uuid(request: HttpRequest, finding_uuid) -> HttpResponse:
    finding = get_object_or_404(Finding, uuid=finding_uuid)
    from django.contrib.auth.models import (  # pylint: disable=import-outside-toplevel
        User,
    )

    assignee_list = User.objects.all()
    context = {"finding": finding, "assignee_list": assignee_list}
    return render(request, "triage/findings_show.html", context)


@login_required
@require_http_methods(["POST"])
def api_update_finding(request: HttpRequest) -> JsonResponse:
    """Updates a Finding."""

    finding_uuid = request.POST.get("finding_uuid")
    finding = get_object_or_404(Finding, uuid=finding_uuid)
    # if not finding.can_edit(request.user):
    #    return HttpResponseForbidden()

    # Modify only these fields, if provided
    permitted_fields = [
        "analyst_impact",
        "confidence",
        "analyst_severity_level",
        "assigned_to",
        "estimated_impact",
    ]
    is_modified = False
    for field in permitted_fields:
        if field in request.POST:
            value = request.POST.get(field)
            if field == "assigned_to":
                if value == "$self":  # Special case: set to current user
                    value = request.user
                if value == "$clear":  # Special case: clear the field
                    value = None
                else:
                    value = get_user_model().objects.filter(username=value).first()
                    if value is None:
                        continue  # No action, invalid user passed in

            if getattr(finding, field) != value:
                setattr(finding, field, value)
                is_modified = True

    if is_modified:
        finding.save()
        return JsonResponse({"status": "ok"})
    else:
        return JsonResponse({"status": "ok, not modified"})


@login_required
@cache_page(60 * 30)
def api_get_source_code(request: HttpRequest) -> JsonResponse:
    """Returns the source code for a finding."""
    file_uuid = request.GET.get("file_uuid")
    if file_uuid:
        file = File.objects.filter(uuid=file_uuid).first()
        if file and file.file_key:
            file_manager = FileManager()
            content = file_manager.get_file(file.file_key)
            if content is not None:
                return JsonResponse(
                    {
                        "file_contents": b64encode(content).decode("utf-8"),
                        "file_name": file.path,
                        "status": "ok",
                    },
                )
    logger.info("Source code not found for %s", file_uuid)
    return JsonResponse({"status": "error", "message": "File not found"}, status=404)


@login_required
@cache_page(60 * 5)
def api_get_files(request: HttpRequest) -> JsonResponse:
    """Returns a list of files related to a finding."""
    project_version_uuid = request.GET.get("project_version_uuid")
    project_version = get_object_or_404(ProjectVersion, uuid=project_version_uuid)

    source_graph = path_to_graph(
        project_version.files.all(),
        project_version.package_url,
        separator="/",
        root=str(project_version.package_url),
    )

    return JsonResponse({"data": source_graph, "status": "ok"})


@login_required
def api_download_file(request: HttpRequest) -> HttpResponse:
    """Downloads a particular file (raw).

    Not currently implemented.
    """
    return HttpResponse("Not implemented.")


def api_upload_attachment(request: HttpRequest) -> JsonResponse:
    """Handles uploads (attachments)"""
    target_type = request.POST.get("target_type")
    target_uuid = request.POST.get("target_uuid")
    if target_uuid is None or target_uuid == "":
        return JsonResponse({"error": "No target_uuid provided"})

    if target_type == "case":
        obj = get_object_or_404(Case, uuid=target_uuid)
    else:
        return JsonResponse({"error": "Invalid target_type"})

    attachments = request.FILES.getlist("attachment")
    results = []
    for attachment in attachments:
        new_attachment = obj.attachments.create(
            filename=attachment.name,
            content_type=attachment.content_type,
            content=attachment.read(),
        )
        results.append(
            {"filename": new_attachment.filename, "uuid": new_attachment.uuid},
        )

    return JsonResponse({"success": True, "attachments": results})
