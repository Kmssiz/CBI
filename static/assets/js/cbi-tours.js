/*
 * Contenu des visites guidées (texte uniquement, aucune logique).
 * Le moteur qui les joue est cbi-tour.js. Les visites ne concernent que les utilisateurs non-admin
 * (base.html ne charge rien pour les admins) et chaque étape suit les droits de l'utilisateur.
 *
 * Une visite :
 *   version  Passer à 2, 3… réaffiche la visite une fois à tout le monde (nouvelle fonction, page remaniée).
 *   label    Nom affiché dans le menu « Visite guidée ».
 *   pages    Noms d'URL Django où la visite est proposée (request.resolver_match.url_name).
 *   noAuto   Pages où la visite générale ne se lance jamais seule (rapport en cours de chargement…).
 *   steps    Les étapes, dans l'ordre.
 *
 * Une étape :
 *   el       Sélecteur CSS de l'élément à montrer. Sans `el`, la bulle s'affiche au centre de l'écran.
 *            Une étape dont l'élément est absent ou invisible est sautée : c'est ce qui adapte la visite
 *            au profil (admin, droit Slider, espaces autorisés…) sans condition à écrire ici.
 *   title    Titre court.   text   Une à deux phrases.
 *   side     top | right | bottom | left (position de la bulle).
 *   drawer   Élément du menu latéral : sur mobile, le menu est ouvert pendant l'étape.
 *   open     L'élément est un <details> : il est déplié pendant l'étape.
 */
(function () {
  'use strict';

  var TOURS = {
    /* ---------- Visite générale : menu, thème, notifications ---------- */
    main: {
      version: 1,
      label: 'Visite générale',
      pages: ['landing'],
      noAuto: ['embed_report', 'embed_custom_report', 'slider_play'],
      steps: [
        {
          title: 'Bienvenue sur le nouveau Portail BI',
          text: 'Cette plateforme remplace l\'ancienne version : plus rapide, plus claire, avec de nouvelles fonctions. ' +
                'Cette visite d\'une minute vous montre où tout se trouve. Vous pourrez la relancer à tout moment.'
        },
        {
          el: '[data-tour="home-access"]',
          side: 'top',
          title: 'Votre espace',
          text: 'Ces cartes vous mènent directement à vos rapports Power BI. Elles dépendent de votre profil : ' +
                'vous ne voyez que ce à quoi vous avez accès.'
        },
        {
          el: '[data-tour="home-notifications"]',
          side: 'left',
          title: 'Notifications',
          text: 'Cliquez sur la cloche pour lire vos dernières notifications : nouveaux rapports, accès accordés, ' +
                'réponses à vos tickets. Un point sombre signale celles que vous n\'avez pas encore lues.'
        },
        {
          el: '[data-tour="nav-home"]', drawer: true, side: 'right',
          title: 'Menu principal',
          text: 'Le menu reste à gauche sur toutes les pages. « Accueil » vous ramène ici.'
        },
        {
          el: '[data-tour="nav-reports"]', drawer: true, open: true, side: 'right',
          title: 'Tableau de bord : vos rapports',
          text: 'Vos rapports Power BI sont rangés par espace. Voici ceux auxquels votre profil donne accès.'
        },
        /* Un pas par espace : seuls ceux que l'utilisateur peut ouvrir sont présentés. */
        {
          el: '[data-tour="space-direction"]', drawer: true, side: 'right',
          title: 'Direction',
          text: 'Les rapports de votre direction.'
        },
        {
          el: '[data-tour="space-pole"]', drawer: true, side: 'right',
          title: 'Pôle',
          text: 'Les rapports de votre pôle.'
        },
        {
          el: '[data-tour="space-consolide"]', drawer: true, side: 'right',
          title: 'Consolidé',
          text: 'Les rapports consolidés.'
        },
        {
          el: '[data-tour="space-biblio"]', drawer: true, side: 'right',
          title: 'Bibliothèque',
          text: 'La bibliothèque : les rapports et extractions disponibles selon vos droits.'
        },
        {
          el: '[data-tour="space-module"]', drawer: true, side: 'right',
          title: 'Module',
          text: 'Les rapports organisés par module.'
        },
        {
          el: '[data-tour="space-anomalie"]', drawer: true, side: 'right',
          title: 'Anomalie',
          text: 'Les rapports d\'anomalies, pour suivre les alertes sur vos données.'
        },
        {
          el: '[data-tour="nav-slider"]', drawer: true, side: 'right',
          title: 'Nouveau : le Slider',
          text: 'Faites défiler plusieurs rapports en plein écran, en boucle : parfait pour une présentation ou un écran mural.'
        },
        {
          el: '[data-tour="nav-tickets"]', drawer: true, side: 'right',
          title: 'Tickets',
          text: 'Un bug, une question, une demande d\'accès ? Créez un ticket et suivez la réponse de l\'équipe.'
        },
        {
          el: '#theme-toggle', drawer: true, side: 'right',
          title: 'Mode clair ou sombre',
          text: 'Choisissez l\'affichage qui vous convient. Votre choix est mémorisé.'
        },
        {
          el: '#tour-launcher', drawer: true, side: 'right',
          title: 'Revoir cette visite',
          text: 'Ce bouton relance cette visite, ou une visite propre à la page que vous consultez.'
        }
      ]
    },

    /* ---------- Espaces de rapports (Direction, Pôle, Consolidé, Bibliothèque, Module, Anomalie) ---------- */
    folders: {
      version: 1,
      label: 'Cette page : vos rapports',
      pages: ['custom_direction', 'custom_pole', 'custom_consolide', 'custom_biblio', 'custom_module',
              'custom_anomalie', 'custom_folder_detail'],
      steps: [
        {
          el: '[data-tour="folder-grid"]', side: 'top',
          title: 'Dossiers et rapports',
          text: 'Cliquez sur un dossier pour l\'ouvrir, puis sur un rapport pour l\'afficher. ' +
                'Le bouton « Retour » remonte d\'un niveau.'
        },
        {
          el: '[data-tour="folder-all"]', side: 'bottom',
          title: 'Tous les rapports',
          text: 'Une liste à plat de tous les rapports de cet espace, pratique pour retrouver un rapport précis.'
        }
      ]
    },

    /* ---------- Rapport ouvert ---------- */
    embed: {
      version: 1,
      label: 'Cette page : le rapport',
      pages: ['embed_report', 'embed_custom_report'],
      steps: [
        {
          el: '#backBtn', side: 'bottom',
          title: 'Retour',
          text: 'Revient à la page précédente.'
        },
        {
          el: '#printBtn', side: 'bottom',
          title: 'Imprimer',
          text: 'Imprime le rapport tel qu\'il est affiché.'
        },
        {
          el: '#shareBtn', side: 'bottom',
          title: 'Partager',
          text: 'Copie le lien du rapport. La personne qui le reçoit doit avoir accès à ce rapport pour l\'ouvrir.'
        },
        {
          el: '#fullscreenBtn', side: 'left',
          title: 'Plein écran',
          text: 'Affiche le rapport sur tout l\'écran. Touche Échap pour revenir.'
        },
        {
          el: '#reportStage', side: 'top',
          title: 'Le rapport',
          text: 'Filtres, segments et info-bulles fonctionnent comme dans Power BI : cliquez directement dans le rapport.'
        },
        {
          el: '.report-strip-skin', side: 'top',
          title: 'Les pages du rapport',
          text: 'Les onglets en bas, sur fond vert, changent de page. Le rapport n\'est chargé qu\'une fois : ' +
                'passer d\'une page à l\'autre est immédiat.'
        }
      ]
    },

    /* ---------- Sliders ---------- */
    sliders: {
      version: 1,
      label: 'Cette page : les sliders',
      pages: ['slider_list'],
      steps: [
        {
          el: 'a[href$="/slider/new/"]', side: 'bottom',
          title: 'Nouveau slider',
          text: 'Choisissez les rapports à enchaîner et la durée d\'affichage de chacun, puis enregistrez.'
        },
        {
          el: '[data-tour="slider-card"]', side: 'right',
          title: 'Votre slider',
          text: 'Le nombre de rapports et la durée par rapport sont indiqués. Le crayon et la corbeille, ' +
                'visibles au survol, modifient ou suppriment le slider.'
        },
        {
          el: '[data-tour="slider-card"] a[href$="/play/"]', side: 'top',
          title: 'Lancer',
          text: 'Lance le slider en boucle, en plein écran. Les données sont rafraîchies automatiquement.'
        }
      ]
    },

    /* ---------- Tickets ---------- */
    tickets: {
      version: 1,
      label: 'Cette page : les tickets',
      pages: ['ticket_list'],
      steps: [
        {
          el: 'a[href$="/tickets/create/"]', side: 'bottom',
          title: 'Nouveau ticket',
          text: 'Signalez un bug, demandez un accès ou une évolution. Plus la description est précise, plus la réponse est rapide.'
        },
        {
          el: '[data-tour="ticket-table"]', side: 'top',
          title: 'Suivi de vos demandes',
          text: 'Retrouvez ici l\'état de chaque ticket, sa priorité et la personne qui s\'en occupe. ' +
                'Cliquez sur un titre pour lire les échanges et répondre.'
        }
      ]
    },

    /* ---------- Notifications ---------- */
    notifications: {
      version: 1,
      label: 'Cette page : les notifications',
      pages: ['notifications'],
      steps: [
        {
          el: '#filterButtons', side: 'bottom',
          title: 'Filtrer et trier',
          text: 'Affichez toutes vos notifications, les lues ou seulement celles que vous n\'avez pas lues. ' +
                'Les boutons « Tout marquer » et « Tout effacer » agissent sur l\'ensemble.'
        },
        {
          el: '#notificationsContainer', side: 'top',
          title: 'Vos notifications',
          text: 'Cliquez sur une notification pour la lire en détail.'
        }
      ]
    }
  };

  window.CBI_TOURS = TOURS;
})();
