# Trying the "new user" scenario

What the app does when a new person arrives, and how to rehearse it without waiting a day.

## What happens for real

| # | Who | What |
|---|-----|------|
| 1 | New person | Logs in with their AD account. The app creates the account with the plain `user` role, **no view rights and no report access**. They see an almost empty app. No notification is sent at this moment. |
| 2 | App (every hour) | `sync_odoo_profiles` looks for people who are in the directory **and in Odoo** (matched by AD identifier / username, or by exact first + last name) and who joined after the cut-off date (`OdooSettings.new_users_since`). Each one becomes a **"Nouveau utilisateur"** alert. |
| 3 | App (once a day per admin) | `new_users_digest` sends every admin one notification: *"Nouveaux utilisateurs : N personne(s) à examiner"*, linking to `/new-users/`. |
| 4 | Admin | Bell icon → the notification → **Nouveaux utilisateurs** page → decides what the person gets (below). |
| 5 | App | Nothing is ever granted automatically. |

So in real life the admin can wait up to an hour (Odoo match) and then until the next digest. Someone who is **not in Odoo** is never announced.

## Rehearsing it now

The commands run inside the app container. After deploying this version:

```bash
docker exec PBI python manage.py simulate_new_user --help
```

### A. With a real test account (recommended: you can go all the way)

You need an AD account that is not used by anyone, and that has logged in to the app **once** (that login is what creates the account).

1. **Log in once** with the test account, in a private window, then log out. It now exists, with no access.
2. **Flag it as new** and notify the admins immediately (no Odoo, no waiting for the digest):
   ```bash
   docker exec PBI python manage.py simulate_new_user --username the.test.account
   ```
3. **As an admin**: the bell now shows *"Nouveaux utilisateurs : 1 personne à examiner"*. Click it (or open **Admin → Utilisateurs**, then the **Nouveaux utilisateurs** button, `/new-users/`).
4. **Give access**, either way:
   * **Pick reports**: on the person's row, the open-padlock button *Accorder des rapports*, tick the reports, *Ajouter*.
   * **Copy a colleague**: open the person's *Accès* page, press **Copier**, choose *Donner à … les accès d'une autre personne*, type the colleague's name, keep *Accès aux rapports*, *Rôles RLS* and *Sections visibles* ticked, **Voir l'aperçu**, check the list, **Confirmer la copie**.
5. Back on `/new-users/`, press **Tout marquer comme vu** (or tick the person and press *Marquer la sélection comme vue*): the person leaves the waiting list.
6. **As the test account** (private window): log in again. The reports appear under the sections they belong to (Direction, Pôle, Bibliothèque, Module…) and the person got a notification *"Vous avez obtenu l'accès à N rapport(s)"*.
7. **Run it again** whenever you like: step 2 puts the person back in the waiting list.

Clean up afterwards: take the access back from the *Accès* page (remove the reports), and nothing else is left: the alert disappears once you mark the person as seen.

### B. Screens only, no real account

```bash
docker exec PBI python manage.py simulate_new_user --fake "Test Dupont"
```

Creates a fake person who cannot log in, flags them and notifies the admins. Good for seeing the **notification and the review page**.
**Do not grant the fake person any report**: PBIRS has no such account, so a grant would write a policy for someone who does not exist.

```bash
docker exec PBI python manage.py simulate_new_user --cleanup     # removes every fake person
```

## If the notification does not show up

| Symptom | Reason | What to do |
|---------|--------|-----------|
| Nothing after the real flow | The person is not in Odoo, or their name is spelled differently (the match is exact), or two people share the name | Use `simulate_new_user --username …`, or fix the name in Odoo |
| Nothing for a real arrival | The account was created before `new_users_since` | Only people who joined after that date are announced |
| One notification per day only | The digest sends at most one per admin per day | `docker exec PBI python manage.py new_users_digest --force` sends another now |
| The command says "No account" | The person never logged in | Log in once with that account first |

## Useful commands

```bash
docker exec PBI python manage.py sync_odoo_profiles          # run the Odoo match now (what step 2 of "for real" does)
docker exec PBI python manage.py new_users_digest            # send the daily notification now (once a day per admin)
docker exec PBI python manage.py new_users_digest --force    # send it again even if one went out today
```
