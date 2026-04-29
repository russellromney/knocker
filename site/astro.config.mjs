// @ts-check
import { defineConfig } from 'astro/config';
import starlight from '@astrojs/starlight';

export default defineConfig({
  site: 'https://knocker.dev',
  integrations: [
    starlight({
      title: 'Knocker',
      description:
        'Embeddable inbound webhook inbox on SQLite. Store first. Ack fast. Process later.',
      logo: {
        src: './src/assets/logo-transparent.png',
        replacesTitle: true,
      },
      social: [
        { icon: 'github', label: 'GitHub', href: 'https://github.com/russellromney/knocker' },
      ],
      components: {
        SiteTitle: './src/components/SiteTitle.astro',
        SocialIcons: './src/components/SocialIcons.astro',
      },
      sidebar: [
        {
          label: 'Start here',
          items: [
            { label: 'Home', slug: 'index' },
            { label: 'Docs overview', slug: 'docs' },
            { label: 'Getting started', slug: 'getting-started' },
          ],
        },
        {
          label: 'Guides',
          items: [
            { label: 'Framework integration', slug: 'guides/framework-integration' },
            { label: 'Verified ingress', slug: 'guides/verified-ingress' },
            { label: 'Operator surface', slug: 'guides/operators' },
            { label: 'Retention and pruning', slug: 'guides/retention' },
          ],
        },
        {
          label: 'Reference',
          items: [
            { label: 'Python API', slug: 'reference/python' },
          ],
        },
        {
          label: 'Project',
          items: [
            { label: 'Roadmap', slug: 'roadmap' },
          ],
        },
      ],
      customCss: ['./src/styles/custom.css'],
    }),
  ],
});
