/** Same theme the page used with the Tailwind CDN. */
module.exports = {
  darkMode: 'class',
  content: [
    '../../templates/**/*.html',
    '../../*/templates/**/*.html',
    '../../static/assets/js/*.js',
    '../../*/forms.py',
    '../../*/views.py',
    '../../*/views_modules/*.py',
  ],
  theme: {
    extend: {
      colors: {
        'primary': '#a5cf4b',
        'background-dark': '#0a0f14',
        'background-light': '#f8fafc',
        'surface-dark': '#121922',
        'surface-light': '#ffffff',
        'card-dark': '#161d26',
        'card-light': '#f1f5f9',
        'accent-gold': '#d4af37',
        'accent-teal': '#2d6a4f',
        'deep-charcoal': '#1c2229',
        'deep-charcoal-light': '#e2e8f0',
      },
      fontFamily: { display: ['Manrope', 'sans-serif'] },
      borderRadius: { DEFAULT: '0.25rem', lg: '0.5rem', xl: '0.75rem', full: '9999px' },
    },
  },
  plugins: [require('@tailwindcss/forms'), require('@tailwindcss/container-queries')],
};
