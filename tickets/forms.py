from django import forms
from django.core.exceptions import ValidationError

from .models import Ticket, TicketMessage


MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024


class ImageUploadValidationMixin:
    """Reject unexpectedly large image uploads before they reach storage."""

    def clean_attachment(self):
        attachment = self.cleaned_data.get("attachment")
        if attachment and attachment.size > MAX_IMAGE_UPLOAD_BYTES:
            raise ValidationError("L'image ne doit pas dépasser 5 Mo.")
        return attachment


class TicketForm(ImageUploadValidationMixin, forms.ModelForm):
    class Meta:
        model = Ticket
        fields = ["title", "description", "ticket_type", "category", "priority", "attachment"]
        widgets = {
            "title": forms.TextInput(
                attrs={"class": "form-control", "placeholder": "Titre du ticket"}
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 5,
                    "placeholder": "Decrivez votre probleme ou demande...",
                }
            ),
            "ticket_type": forms.Select(attrs={"class": "form-select"}),
            "category": forms.Select(attrs={"class": "form-select"}),
            "priority": forms.Select(attrs={"class": "form-select"}),
            "attachment": forms.FileInput(attrs={"class": "form-control"}),
        }


class TicketMessageForm(ImageUploadValidationMixin, forms.ModelForm):
    class Meta:
        model = TicketMessage
        fields = ["content", "attachment"]
        widgets = {
            "content": forms.Textarea(
                attrs={
                    "class": "w-full bg-slate-50 dark:bg-surface-dark border border-slate-200 dark:border-slate-700/50 rounded-lg px-4 py-3 text-slate-900 dark:text-white placeholder-slate-400 dark:placeholder-slate-600 focus:outline-none focus:border-primary/50 focus:ring-1 focus:ring-primary/50 transition-all resize-y",
                    "rows": 3,
                    "placeholder": "Ecrivez votre message...",
                }
            ),
            "attachment": forms.FileInput(
                attrs={
                    "class": "block w-full text-sm text-slate-500 dark:text-slate-400 file:mr-4 file:py-2.5 file:px-4 file:rounded-lg file:border-0 file:text-sm file:font-semibold file:bg-primary/10 file:text-primary hover:file:bg-primary/20 cursor-pointer pt-2",
                    "accept": "image/*",
                }
            ),
        }
