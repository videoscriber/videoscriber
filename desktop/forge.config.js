module.exports = {
  packagerConfig: {
    name: 'Videoscriber',
    executableName: 'videoscriber',
    icon: './icon', // Electron appends .icns/.ico/.png per platform
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
      'bin',
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
        icon: './icon.icns', // icon for the .app inside the mounted .dmg window
      },
    },
    {
      name: '@electron-forge/maker-zip',
      platforms: ['darwin'],
    },
  ],
};
