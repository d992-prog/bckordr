export interface PortalConfig {
  enabled: boolean;
  browser_login_enabled: boolean;
  mini_app_enabled: boolean;
  login_path: string | null;
  bot_url: string | null;
  trial_enabled: boolean;
  support_text: string;
}

export interface PortalMe {
  display_name: string;
  csrf_token: string;
}

export interface PortalPlan {
  id: number;
  name: string;
  description: string | null;
  duration_days: number | null;
  traffic_limit_gb: number | null;
  max_devices: number;
  price_amount: number;
  currency: string;
  is_trial: boolean;
}

export type PortalTrialState =
  | "disabled"
  | "available"
  | "capacity_paused"
  | "preparing"
  | "active"
  | "used";

export interface PortalTrial {
  state: PortalTrialState;
  duration_days: number;
  profile_limit: number;
  subscription_id: number | null;
  access_key_id: number | null;
  expires_at: string | null;
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
