module.exports = ({ config }) => ({
  ...config,
  android: {
    ...config.android,
    // Permit plain HTTP only in an explicitly opted-in local dev build.
    usesCleartextTraffic: process.env.EXPO_PUBLIC_ALLOW_INSECURE_LOCAL_API === 'true',
  },
});
