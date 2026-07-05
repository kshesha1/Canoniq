// @ts-check
// `@type` JSDoc annotations allow editor autocompletion and type checking
// (when paired with `@ts-check`).
// There are various equivalent ways to declare your Docusaurus config.
// See: https://docusaurus.io/docs/api/docusaurus-config

import {themes as prismThemes} from 'prism-react-renderer';

// This runs in Node.js - Don't use client-side code here (browser APIs, JSX...)

/** @type {import('@docusaurus/types').Config} */
const config = {
  title: 'Canoniq',
  tagline: 'A cold-start semantic bootstrapping engine for brownfield enterprises',
  favicon: 'img/favicon.ico',

  // Future flags, see https://docusaurus.io/docs/api/docusaurus-config#future
  future: {
    v4: true, // Improve compatibility with the upcoming Docusaurus v4
  },

  // Set the production url of your site here
  url: 'https://kshesha1.github.io',
  // Set the /<baseUrl>/ pathname under which your site is served
  // For GitHub pages deployment, it is often '/<projectName>/'
  baseUrl: '/Canoniq/',

  // GitHub pages deployment config.
  organizationName: 'kshesha1', // GitHub org/user name.
  projectName: 'Canoniq', // GitHub repo name.
  deploymentBranch: 'gh-pages', // unused by the Actions-based deploy below, kept for `docusaurus deploy` as a fallback
  trailingSlash: false,

  onBrokenLinks: 'throw',

  // Even if you don't use internationalization, you can use this field to set
  // useful metadata like html lang. For example, if your site is Chinese, you
  // may want to replace "en" with "zh-Hans".
  i18n: {
    defaultLocale: 'en',
    locales: ['en'],
  },

  presets: [
    [
      'classic',
      /** @type {import('@docusaurus/preset-classic').Options} */
      ({
        docs: {
          // Points at the existing docs/ tree next to the canoniq package,
          // rather than a copy under site/ -- docs stay next to the code
          // they describe. See ../docs/README.md.
          path: '../docs',
          routeBasePath: '/', // docs are the whole site, no separate landing page
          sidebarPath: './sidebars.js',
          exclude: ['**/README.md'], // contributor-facing only, not a rendered page
          editUrl: 'https://github.com/kshesha1/Canoniq/tree/main/docs/',
        },
        blog: false, // no blog for this project
        theme: {
          customCss: './src/css/custom.css',
        },
      }),
    ],
  ],

  themeConfig:
    /** @type {import('@docusaurus/preset-classic').ThemeConfig} */
    ({
      image: 'img/social-card.jpg',
      colorMode: {
        respectPrefersColorScheme: true,
      },
      navbar: {
        title: 'Canoniq',
        logo: {
          alt: 'Canoniq Logo',
          src: 'img/logo.svg',
        },
        items: [
          {
            type: 'docSidebar',
            sidebarId: 'docsSidebar',
            position: 'left',
            label: 'Docs',
          },
          {
            href: 'https://github.com/kshesha1/Canoniq',
            label: 'GitHub',
            position: 'right',
          },
        ],
      },
      footer: {
        style: 'dark',
        links: [
          {
            title: 'Docs',
            items: [
              {label: 'Introduction', to: '/'},
              {label: 'Getting started', to: '/getting-started'},
              {label: 'CLI reference', to: '/guides/cli-reference'},
            ],
          },
          {
            title: 'More',
            items: [
              {
                label: 'GitHub',
                href: 'https://github.com/kshesha1/Canoniq',
              },
            ],
          },
        ],
        copyright: `Copyright © ${new Date().getFullYear()} Canoniq. Built with Docusaurus.`,
      },
      prism: {
        theme: prismThemes.github,
        darkTheme: prismThemes.dracula,
      },
    }),
};

export default config;
