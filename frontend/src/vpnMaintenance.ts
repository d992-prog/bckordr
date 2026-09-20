export function isVpnConfigurationActionDisabled({
  vpnInstalled,
  vpnMaintenanceBlocked,
}: {
  vpnInstalled: boolean;
  vpnMaintenanceBlocked: boolean;
}) {
  return !vpnInstalled || vpnMaintenanceBlocked;
}
