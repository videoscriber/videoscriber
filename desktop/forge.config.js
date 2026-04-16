module.exports = {
  packagerConfig: {
    name: 'Videoscriber',
    executableName: 'videoscriber',
    asar: false, // Don't bundle in asar — we need access to python backend files
    extraResource: [
      '../app.py',
      '../transcriber.py',
      '../database.py',
      '../requirements.txt',
      '../templates',
      '../static',
    ],
    osxSign: {},
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
