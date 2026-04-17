module.exports = {
  packagerConfig: {
    name: 'Videoscriber',
    executableName: 'videoscriber',
    asar: false, // Don't bundle in asar — we need access to python backend files
    extraResource: [
      '../app.py',
      '../auth.py',
      '../auth_routes.py',
      '../chat_routes.py',
      '../database.py',
      '../domain_routes.py',
      '../email_domains.py',
      '../email_service.py',
      '../retrieval.py',
      '../sms.py',
      '../transcriber.py',
      '../requirements.txt',
      '../templates',
      '../static',
      'scripts',
    ],
    // osxSign intentionally omitted — unsigned build. Users need to right-click →
    // Open on first launch to bypass Gatekeeper. Add an Apple Developer ID here
    // once we're signing + notarizing.
  },
  makers: [
    {
      name: '@electron-forge/maker-dmg',
      config: {
        format: 'ULFO',
      },
    },
    {
      name: '@electron-forge/maker-zip',
      platforms: ['darwin'],
    },
  ],
};
