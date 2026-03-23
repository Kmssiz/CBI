from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render

from .models import Notification
from users.utils import get_user_permissions


def _is_json_request(request) -> bool:
    content_type = request.headers.get("Content-Type", "")
    return "application/json" in content_type


@login_required
def notifications_list(request):
    notifications = Notification.objects.filter(user=request.user).order_by("-created_at")
    unread = notifications.filter(is_read=False).count()
    permissions = get_user_permissions(request.user)

    return render(
        request,
        "notifications/notifications.html",
        {
            "notifications": notifications,
            "unread": unread,
            "permissions": permissions,
        },
    )


@login_required
def delete_notification(request, id):
    notification = get_object_or_404(Notification, pk=id, user=request.user)

    if request.method == "POST":
        notification.delete()

        if _is_json_request(request):
            return JsonResponse({"success": True})

        return redirect("notifications")

    return render(
        request,
        "notifications/notifications.html",
        {"notifications": Notification.objects.filter(user=request.user)},
    )


@login_required
def delete_all_notifications(request):
    notifications = Notification.objects.filter(user=request.user)

    if request.method == "POST":
        notifications.delete()

        if _is_json_request(request):
            return JsonResponse({"success": True})

        return redirect("notifications")

    return render(request, "notifications/notifications.html", {"notifications": notifications})


@login_required
def mark_notifications(request):
    notifications = Notification.objects.filter(user=request.user)

    if request.method == "POST":
        notifications.update(is_read=True)

        if _is_json_request(request):
            return JsonResponse({"success": True})

        return redirect("notifications")

    return render(request, "notifications/notifications.html", {"notifications": notifications})


@login_required
def mark_as_read(request, notification_id):
    if request.method == "POST":
        notification = get_object_or_404(Notification, id=notification_id, user=request.user)
        notification.is_read = True
        notification.save(update_fields=["is_read"])
        return JsonResponse({"success": True})

    return JsonResponse({"success": False}, status=400)
