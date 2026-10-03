"""Cloud provider metadata (glyphs use Nerd Font code points, like the rest of Omarchy)."""

import re
from typing import Any, Dict, Iterable, List, Optional

PROVIDERS: Dict[str, Dict[str, Any]] = {
    "drive": {
        "name": "Google Drive",
        "rclone_type": "drive",
        "glyph": "󰊭",
        "color": "#4285F4",
        "auth": "oauth",
    },
    "onedrive": {
        "name": "Microsoft OneDrive",
        "rclone_type": "onedrive",
        "glyph": "󰏊",
        "color": "#0078D4",
        "auth": "oauth",
    },
    "dropbox": {
        "name": "Dropbox",
        "rclone_type": "dropbox",
        "glyph": "",
        "color": "#0061FF",
        "auth": "oauth",
    },
    "box": {
        "name": "Box",
        "rclone_type": "box",
        "glyph": "󰉉",
        "color": "#0061D5",
        "auth": "oauth",
    },
    "pcloud": {
        "name": "pCloud",
        "rclone_type": "pcloud",
        "glyph": "󰅟",
        "color": "#14BF96",
        "auth": "oauth",
    },
    "nextcloud": {
        "name": "Nextcloud",
        "rclone_type": "webdav",
        "glyph": "󰒋",
        "color": "#0082C9",
        "auth": "credentials",
    },
    "webdav": {
        "name": "WebDAV",
        "rclone_type": "webdav",
        "glyph": "󰒋",
        "color": "#7E57C2",
        "auth": "credentials",
    },
    "s3": {
        "name": "S3 storage",
        "rclone_type": "s3",
        "glyph": "󰋊",
        "color": "#FF9900",
        "auth": "credentials",
    },
    "protondrive": {
        "name": "Proton Drive",
        "rclone_type": "protondrive",
        "glyph": "󰅟",
        "color": "#6D4AFF",
        "auth": "credentials",
    },
}

GENERIC = {
    "name": "Cloud remote",
    "rclone_type": "",
    "glyph": "󰅟",
    "color": "#A0A0A0",
    "auth": "none",
}

# Remote types signed in through the browser (rclone authorize)
OAUTH_TYPES = {"drive", "onedrive", "dropbox", "box", "pcloud"}

# Step-by-step setup of the user's own OAuth client, for every OAuth backend rclone lets use
# one. Steps follow rclone's docs (rclone 1.75) and, for pCloud, which rclone doesn't cover,
# pCloud's developer site. The UI and `guac set-client-id` both render them. A field "pattern" is a regex the value must match,
# "reject" one it must not (the usual copy-paste mix-up), each with the message to show.
CLIENT_GUIDES: Dict[str, Dict[str, Any]] = {
    "drive": {
        "title": "Your own Google client",
        "required": True,
        "minutes": 5,
        "why": "rclone's built-in Google client is shared by every rclone user, so Google throttles it "
        "(slow listings, \"rate limit exceeded\"), and it stops working during 2026. A client of your "
        "own is free, and any Google account can own it.",
        "steps": [
            {
                "title": "Create a project",
                "text": "Sign in to Google Cloud with any Google account and create a project. "
                "Any name works, for example Guacamole.",
                "link": {"label": "Create a project", "url": "https://console.cloud.google.com/projectcreate"},
            },
            {
                "title": "Enable the Google Drive API",
                "text": "With the new project selected at the top of the page, click Enable.",
                "link": {
                    "label": "Open the Drive API",
                    "url": "https://console.cloud.google.com/apis/library/drive.googleapis.com",
                },
            },
            {
                "title": "Set up the consent screen",
                "text": "Click Get started. App name: anything (rclone is fine). Support email: yours. "
                "Audience: External (Internal only reaches accounts of your own Workspace). Add your "
                "contact email, accept the terms and click Create.",
                "link": {"label": "Open Google Auth Platform", "url": "https://console.cloud.google.com/auth/overview"},
            },
            {
                "title": "Add the Drive scopes",
                "text": "Click Add or remove scopes, paste these into Manually add scopes, click Add to "
                "table and Update, then Save at the bottom of the page.",
                "link": {"label": "Open Data access", "url": "https://console.cloud.google.com/auth/scopes"},
                "copy": [
                    {
                        "label": "Scopes",
                        "value": "https://www.googleapis.com/auth/docs,"
                        "https://www.googleapis.com/auth/drive,"
                        "https://www.googleapis.com/auth/drive.metadata.readonly",
                    }
                ],
            },
            {
                "title": "Publish the app",
                "text": "Click Publish app and confirm. Google's review is not needed for personal use. "
                "An app left in Testing works too, but its sign-in expires every 7 days; if you keep it "
                "there, add yourself under Test users.",
                "link": {"label": "Open Audience", "url": "https://console.cloud.google.com/auth/audience"},
            },
            {
                "title": "Create the client",
                "text": "Application type: Desktop app, any name, then Create. Copy the client ID and "
                "the client secret it shows into the fields below.",
                "link": {"label": "Create a client", "url": "https://console.cloud.google.com/auth/clients/create"},
            },
        ],
        "signin_note": "Google warns that it hasn't verified the app. It is your own: choose "
        "Advanced, then Go to … (unsafe).",
        "id": {
            "label": "Client ID",
            "placeholder": "1234567890-abc123.apps.googleusercontent.com",
            "pattern": r"^[0-9]+-[0-9A-Za-z_]+\.apps\.googleusercontent\.com$",
            "error": "A Google client ID ends in .apps.googleusercontent.com",
        },
        "secret": {
            "label": "Client secret",
            "placeholder": "GOCSPX-…",
            "pattern": r"^[0-9A-Za-z_-]{16,}$",
            "error": "That doesn't look like a client secret",
        },
        "docs": "https://rclone.org/drive/#making-your-own-client-id",
    },
    "dropbox": {
        "title": "Your own Dropbox app",
        "required": False,
        "minutes": 3,
        "why": "rclone's built-in Dropbox app key is shared by every rclone user. An app of your own "
        "gets limits of its own. It is free, and any Dropbox account can own it.",
        "steps": [
            {
                "title": "Create an app",
                "text": "Choose Scoped access, then Full Dropbox (App folder would limit the drive to a "
                "single folder; team folders need Full Dropbox). App names are global, so pick a "
                "unique one, not rclone. Click Create app.",
                "link": {"label": "Open the App Console", "url": "https://www.dropbox.com/developers/apps/create"},
            },
            {
                "title": "Grant the permissions",
                "text": "On the Permissions tab tick account_info.read, files.metadata.write, "
                "files.content.write, files.content.read and sharing.write (the matching read boxes "
                "tick themselves), then click Submit.",
            },
            {
                "title": "Add the redirect URI",
                "text": "On the Settings tab, add this address under OAuth2 Redirect URIs.",
                "copy": [{"label": "Redirect URI", "value": "http://localhost:53682/"}],
            },
            {
                "title": "Copy the key and secret",
                "text": "Also on the Settings tab: the App key is the client ID and the App secret "
                "(click Show) is the client secret.",
            },
        ],
        "signin_note": "",
        "id": {
            "label": "App key",
            "placeholder": "App key",
            "pattern": r"^[0-9A-Za-z]{8,40}$",
            "error": "A Dropbox app key is a short code of letters and digits",
        },
        "secret": {
            "label": "App secret",
            "placeholder": "App secret",
            "pattern": r"^[0-9A-Za-z]{8,40}$",
            "error": "A Dropbox app secret is a short code of letters and digits",
        },
        "docs": "https://rclone.org/dropbox/#get-your-own-dropbox-app-id",
    },
    "onedrive": {
        "title": "Your own OneDrive client",
        "required": False,
        "minutes": 5,
        "why": "rclone's built-in OneDrive client is shared by every rclone user and can get "
        "throttled. Registering your own is free; Azure asks you to create a free account first.",
        "steps": [
            {
                "title": "Register an app",
                "text": "In Microsoft Entra ID choose Add, then App registration. Any name. Supported "
                "accounts: any organizational directory and personal Microsoft accounts. Redirect URI: "
                "platform Web, with this address. Click Register.",
                "link": {
                    "label": "Open the Azure portal",
                    "url": "https://portal.azure.com/?quickstart=true#view/Microsoft_AAD_IAM/ActiveDirectoryMenuBlade/~/Overview",
                },
                "copy": [{"label": "Redirect URI", "value": "http://localhost:53682/"}],
            },
            {
                "title": "Copy the client ID",
                "text": "On the app's Overview, copy the Application (client) ID.",
            },
            {
                "title": "Create a client secret",
                "text": "Certificates & secrets, New client secret, expiry 24 months. Copy its Value "
                "right away: it is shown only once (the Secret ID is not the secret).",
            },
            {
                "title": "Grant the permissions",
                "text": "API permissions, Add a permission, Microsoft Graph, Delegated permissions. Tick "
                "Files.Read, Files.ReadWrite, Files.Read.All, Files.ReadWrite.All, offline_access, "
                "User.Read and Sites.Read.All, then Add permissions.",
            },
        ],
        "signin_note": "Work and school accounts may need an administrator to approve the app. The "
        "secret expires after 24 months: set a new one here then.",
        "id": {
            "label": "Application (client) ID",
            "placeholder": "00000000-0000-0000-0000-000000000000",
            "pattern": r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
            "error": "The application ID looks like 00000000-0000-0000-0000-000000000000",
        },
        "secret": {
            "label": "Client secret value",
            "placeholder": "Secret value",
            "pattern": r"^\S{16,}$",
            "error": "That doesn't look like a secret value",
            "reject": r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$",
            "reject_error": "That is the Secret ID. Copy the secret's Value instead",
        },
        "docs": "https://rclone.org/onedrive/#getting-your-own-client-id-and-key",
    },
    "box": {
        "title": "Your own Box app",
        "required": False,
        "minutes": 3,
        "why": "Box limits API calls per app, and rclone's built-in app is shared by every rclone "
        "user. An app of your own is free.",
        "steps": [
            {
                "title": "Create a custom app",
                "text": "My Apps, Create New App, Custom App. Any name; for Purpose choose Automation. "
                "Click Next, choose User Authentication (OAuth 2.0), then Create App.",
                "link": {"label": "Open the Developer Console", "url": "https://app.box.com/developers/console"},
            },
            {
                "title": "Add the redirect URI",
                "text": "On the Configuration tab, add this address under OAuth 2.0 Redirect URI.",
                "copy": [{"label": "Redirect URI", "value": "http://127.0.0.1:53682/"}],
            },
            {
                "title": "Set the scopes",
                "text": "Under Application Scopes tick Read all files and folders stored in Box and Write "
                "all files and folders stored in Box. Leave the rest, then Save Changes.",
            },
            {
                "title": "Copy the credentials",
                "text": "Copy the Client ID and Client Secret from the same Configuration tab.",
            },
        ],
        "signin_note": "",
        "id": {
            "label": "Client ID",
            "placeholder": "Client ID",
            "pattern": r"^[0-9A-Za-z]{16,64}$",
            "error": "A Box client ID is a long code of letters and digits",
        },
        "secret": {
            "label": "Client secret",
            "placeholder": "Client secret",
            "pattern": r"^[0-9A-Za-z]{16,64}$",
            "error": "A Box client secret is a long code of letters and digits",
        },
        "docs": "https://rclone.org/box/#get-your-own-box-app-id",
    },
    "pcloud": {
        "title": "Your own pCloud app",
        "required": False,
        "minutes": 3,
        "why": "rclone's built-in pCloud app is shared by every rclone user. An app of your own is "
        "free, and any pCloud account can own it.",
        "steps": [
            {
                "title": "Create an app",
                "text": "Sign in to pCloud's developer site with your pCloud account and create a new app "
                "with a unique name. Give it access to all folders, with write access.",
                "link": {"label": "Open My applications", "url": "https://docs.pcloud.com/my_apps/"},
            },
            {
                "title": "Add the redirect URI",
                "text": "In the app's settings, add this address under Redirect URIs and save. Without it "
                "pCloud refuses the sign-in (\"redirect_uri is not authorized\").",
                "copy": [{"label": "Redirect URI", "value": "http://localhost:53682/"}],
            },
            {
                "title": "Copy the credentials",
                "text": "The app's page shows its Client ID and Client secret.",
            },
        ],
        "signin_note": "US and EU accounts both work: the right pCloud server is found after you sign in.",
        "id": {
            "label": "Client ID",
            "placeholder": "Client ID",
            "pattern": r"^[0-9A-Za-z]{6,64}$",
            "error": "A pCloud client ID is a short code of letters and digits",
        },
        "secret": {
            "label": "Client secret",
            "placeholder": "Client secret",
            "pattern": r"^[0-9A-Za-z]{6,64}$",
            "error": "A pCloud client secret is a code of letters and digits",
        },
        "docs": "https://rclone.org/pcloud/",
    },
}


def detect(remote: Dict[str, Any]) -> Dict[str, Any]:
    rtype = str(remote.get("type", "")).lower()
    vendor = str(remote.get("vendor", "")).lower()
    if rtype == "webdav" and vendor in ("nextcloud", "owncloud"):
        key = "nextcloud"
    else:
        key = rtype
    meta = dict(PROVIDERS.get(key, GENERIC))
    meta["id"] = key if key in PROVIDERS else "generic"
    if meta["id"] == "generic":
        meta["name"] = rtype.capitalize() if rtype else GENERIC["name"]
        meta["rclone_type"] = rtype
    return meta


def client_guide(rtype: str) -> Optional[Dict[str, Any]]:
    guide = CLIENT_GUIDES.get(rtype)
    if guide is None:
        return None
    return dict(guide, provider=rtype, name=PROVIDERS[rtype]["name"])


def check_client(rtype: str, client_id: str, client_secret: str) -> str:
    """Why a pasted client id/secret pair can't be right ("" when it looks fine)."""
    guide = CLIENT_GUIDES.get(rtype)
    if not client_id or not client_secret:
        return "Both the client ID and the client secret are needed"
    if guide is None:
        return ""
    for value, spec in ((client_id, guide["id"]), (client_secret, guide["secret"])):
        if spec.get("reject") and re.search(spec["reject"], value):
            return spec["reject_error"]
        if not re.search(spec["pattern"], value):
            return spec["error"]
    if client_id == client_secret:
        return "The client ID and the secret are the same: paste each into its own field"
    return ""


def remote_warnings(remote: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Problems worth showing on a drive card, derived from its rclone config (never exposes secrets)."""
    rtype = str(remote.get("type", "")).lower()
    if rtype not in CLIENT_GUIDES or str(remote.get("client_id", "")).strip():
        return []
    if rtype == "drive":
        return [
            {
                "code": "shared_client_id",
                "level": "warning",
                "dismissible": False,
                "text": "Uses rclone's shared Google client: heavily throttled, and it stops working "
                "during 2026. Set up your own (about 5 minutes).",
            }
        ]
    guide = CLIENT_GUIDES[rtype]
    return [
        {
            "code": "shared_app",
            "level": "warning",
            "dismissible": True,
            "text": f"Uses rclone's built-in {PROVIDERS[rtype]['name']} app, shared by every rclone user and "
            f"throttled with them. Set up your own (about {guide['minutes']} minutes).",
        }
    ]


def visible_warnings(warnings: Iterable[Dict[str, Any]], hidden: Iterable[str]) -> List[Dict[str, Any]]:
    hidden = set(hidden)
    return [w for w in warnings if not (w.get("dismissible") and w["code"] in hidden)]


def bisync_remote_spec(remote: str, rtype: str, path: str) -> str:
    """The remote side of a folder kept local, with backend options bisync needs."""
    opts = ""
    if rtype == "drive":
        # Google Docs have no real size and can't round-trip; bisync must skip them
        opts = ",skip_gdocs=true"
    return f"{remote}{opts}:{path}"
