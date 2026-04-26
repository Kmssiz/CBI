# PBI Web Application - Design & Architecture

## 1. Project Overview
PBI is a robust Django-based web application that acts as a custom portal for Power BI Report Server (PBIRS). It provides enhanced user and role management, custom virtual folder organization, dynamic dashboards, and LDAP/Active Directory integration, wrapped in a premium, modern, and responsive user interface.

## 2. Technology Stack
- **Backend Framework:** Django 5.1+
- **Frontend Technologies:** 
  - Tailwind CSS (via CDN for rapid utility classes and responsive layouts)
  - Bootstrap (for specific components and legacy compatibility)
  - Vanilla JavaScript
  - Chart.js (for dashboard analytics and data visualization)
- **Authentication:** LDAP (Lightweight Directory Access Protocol) via `ldap3`
- **Database:** SQLite (Development) / PostgreSQL (Production)
- **Infrastructure:** Docker & Docker Compose (separated web and background sync services)

## 3. Architecture & Key Patterns

### 3.1. Service Layer Pattern
To ensure modularity and maintainability, all interactions with the PBIRS REST API v2.0 are abstracted through a central Service Layer (`powerbi_report/services/pbirs_client.py`).
- **`PBIRSClient`**: Handles all API requests, authentication headers (NTLM), caching, and error logging.
- **Benefits**: Prevents Django views from being bloated with direct API calls, centralizes error handling, and creates a single source of truth for Power BI integrations.

### 3.2. Asynchronous Permission Synchronization
PBIRS permission synchronization is offloaded to a separate, dedicated container (`PBI_PERMISSION_SYNC`).
- When a user's role or permissions change in Django, the background service synchronizes these access control lists (ACLs) to the PBIRS instance.
- This decouples the frontend from potentially slow API calls to PBIRS, keeping the UI snappy and ensuring the web server remains unblocked.

## 4. Authentication & Security

### 4.1. LDAP & NTLM
The application uses Active Directory/LDAP to authenticate users. Since PBIRS itself requires NTLM authentication (which needs raw user credentials rather than standard tokens or service accounts), a specific architectural choice was made:
- **Session-Based Password Storage**: Passwords are intentionally and securely stored in the encrypted Django user session temporarily. This allows the `PBIRSClient` to transparently authenticate requests to the PBIRS backend on behalf of the user. Passwords are *never* stored in the database.

### 4.2. Role-Based Access Control (RBAC)
- Custom Django permissions are mapped to PBIRS roles (e.g., "Explorateur").
- Standard roles (`admin`, `user`) dictate UI visibility and access within the custom portal.

## 5. UI / UX Design System

The application features a "Premium" aesthetic, heavily focusing on a polished user experience with dynamic interactions.

### 5.1. Typography & Icons
- **Font**: `Manrope` (Google Fonts) for clean, modern readability.
- **Icons**: `Material Symbols Outlined` with fine-tuned font-variation settings (wght 300, opsz 24) for elegant, thin-line iconography.

### 5.2. Color Palette & Theming
The app ships with a comprehensive Light and Dark mode, managed primarily through Tailwind CSS and custom CSS variables in `base.html`.
- **Primary**: Deep Blue (`#137fec` / `#2563eb`)
- **Backgrounds (Dark)**: Deep charcoals (`#0a0f14`, `#121922`, `#161d26`)
- **Backgrounds (Light)**: Clean slates (`#f8fafc`, `#ffffff`)
- **Accents**: Emerald (success/grants), Red (errors/revokes), Indigo/Purple (highlights), Gold (`#d4af37`).

### 5.3. Design Components
- **Glassmorphism**: Heavy use of `backdrop-blur` and translucent background colors (`bg-white/80`, `dark:bg-slate-900/10`) for sticky headers and modals.
- **Gradients & Textures**: A subtle mesh gradient (`bg-mesh`) is used globally to add depth.
- **Custom Scrollbars**: Overridden Webkit scrollbars (`custom-scrollbar`) designed to blend seamlessly into both light and dark themes.
- **Card-Theme**: A standardized `.card-theme` CSS class providing consistent shadows, borders, and transitions across UI panels.
- **Animations**: Subtle micro-interactions, like scale on hover, opacity fades on buttons, and smooth transition-all utilities to make the app feel alive and responsive.

### 5.4. CSS Conflicts Management
Due to the hybrid use of Bootstrap and Tailwind CSS, specific care is taken in the codebase to override Bootstrap's `!important` classes (e.g., `.bg-white`). Tailwind's dark mode classes are appended with the `!` modifier (e.g., `dark:!bg-slate-800`) to ensure proper specificity when toggling themes.

## 6. Directory Structure
```text
CBI/
├── powerbi_report/         # PBIRS integration app
│   ├── services/           # External API Clients (pbirs_client.py)
│   ├── views.py            # Portal rendering
│   └── models.py           # Report metadata models
├── users/                  # Custom authentication and RBAC
│   ├── ldap_utils.py       # AD Integration logic
│   ├── views.py            # User/Role management UI
│   └── models.py           # CustomUser, Roles
├── templates/              # Global templates (base.html, landing.html)
├── static/                 # CSS, JS, Fonts, Images
├── docker-compose.yml      # Service orchestration
└── design.md               # This document
```
