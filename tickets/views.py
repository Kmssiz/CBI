from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models
from .models import Ticket
from .forms import TicketForm

@login_required
def ticket_list(request):
    if request.user.is_superuser or request.user.groups.filter(name='admin').exists():
        tickets = Ticket.objects.all()
    else:
        tickets = Ticket.objects.filter(created_by=request.user)
    
    return render(request, 'tickets/ticket_list.html', {'tickets': tickets})

@login_required
def create_ticket(request):
    if request.method == 'POST':
        form = TicketForm(request.POST)
        if form.is_valid():
            ticket = form.save(commit=False)
            ticket.created_by = request.user
            ticket.save()
            messages.success(request, 'Ticket created successfully.')
            return redirect('tickets:ticket_list')
    else:
        form = TicketForm()
    
    return render(request, 'tickets/create_ticket.html', {'form': form})

@login_required
def ticket_detail(request, ticket_id):
    ticket = get_object_or_404(Ticket, id=ticket_id)
    # Allow access if user is admin or creator
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists() or ticket.created_by == request.user):
        messages.error(request, "You do not have permission to view this ticket.")
        return redirect('tickets:ticket_list')

    from django.contrib.auth import get_user_model
    User = get_user_model()
    # Get users who are superusers or in the admin group
    admins = User.objects.filter(models.Q(is_superuser=True) | models.Q(groups__name='admin')).distinct()
    
    return render(request, 'tickets/ticket_detail.html', {'ticket': ticket, 'admins': admins})

@login_required
def assign_ticket(request, ticket_id):
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists()):
        messages.error(request, "You do not have permission to perform this action.")
        return redirect('tickets:ticket_list')
        
    ticket = get_object_or_404(Ticket, id=ticket_id)
    
    if request.method == 'POST':
        user_id = request.POST.get('assigned_to')
        if user_id:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                user = User.objects.get(id=user_id)
                ticket.assigned_to = user
                ticket.save()
                messages.success(request, f'Ticket assigned to {user.get_full_name() or user.username}.')
            except User.DoesNotExist:
                messages.error(request, 'User not found.')
        else:
            ticket.assigned_to = None
            ticket.save()
            messages.success(request, 'Ticket unassigned.')
            
    return redirect('tickets:ticket_detail', ticket_id=ticket.id)

@login_required
def change_status(request, ticket_id):
    if not (request.user.is_superuser or request.user.groups.filter(name='admin').exists()):
        messages.error(request, "You do not have permission to perform this action.")
        return redirect('tickets:ticket_list')
        
    ticket = get_object_or_404(Ticket, id=ticket_id)
    
    if request.method == 'POST':
        new_status = request.POST.get('status')
        valid_statuses = [s[0] for s in Ticket.STATUS_CHOICES]
        if new_status in valid_statuses:
            ticket.status = new_status
            ticket.save()
            messages.success(request, f'Ticket status updated to {ticket.get_status_display()}.')
        else:
            messages.error(request, 'Invalid status.')
            
    return redirect('tickets:ticket_detail', ticket_id=ticket.id)
