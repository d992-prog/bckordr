export interface PortalConfig {
  enabled: boolean;
  browser_login_enabled: boolean;
  mini_app_enabled: boolean;
  login_path: string | null;
  support_text: string;
}

export interface PortalMe {
  display_name: string;
  csrf_token: string;
}

export interface PortalSubscription {
  id: number;
  service_name: string;
  state: string;
  starts_at: string | null;
  expires_at: string | null;
  profile_limit: number;
  profiles_used: number;
  traffic_limit_gb_per_profile: number | null;
}

export interface PortalProfile {
  id: number;
  subscription_id: number;
  display_name: string;
  state: string;
  can_connect: boolean;
}

export interface PortalConnection {
  uri: string;
}
