# Agentra Dashboard UI Design Document

## Overview
This document outlines the design for modernizing the Agentra dashboard UI following current SaaS UX patterns.

## Design Goals
1. Modern, clean SaaS interface inspired by Vercel, Linear, and similar tools
2. Full light/dark theme support with user preference persistence
3. Account settings modal/page for user configuration
4. Maintain all existing functionality while improving visual hierarchy
5. Mobile-responsive design with accessibility support

## Architecture Changes

### Theme System
- Extend existing CSS custom properties system
- Add light theme variables alongside existing dark theme
- Create React context for theme management
- Persist theme preference to localStorage
- Support system/light/dark theme options

### Component Structure
```
App.tsx (updated)
├── ThemeProvider (new)
├── Header (redesigned)
│   ├── Logo/Brand
│   ├── Theme Toggle Button (new)
│   └── Account Settings Button (new)
├── AccountSettings Modal (new)
│   ├── Profile Section
│   ├── GitHub Connector (moved from Header)
│   └── Theme Preference Settings
├── Tabs (existing)
└── Panels (existing, with visual updates)
```

### Visual Design Updates

#### Header Redesign
- Cleaner layout with better spacing
- Modern glass effect with subtle backdrop blur
- Theme toggle icon button (sun/moon icons)
- Account settings access button
- Remove GitHub status from header (move to settings)
- Keep pause/resume functionality prominently placed

#### Account Settings Modal
- Modern modal overlay with backdrop blur
- Sidebar navigation for different sections
- Profile section showing "Logged in as orchestrator"
- GitHub Connector section with existing functionality
- Theme preference section with radio buttons (System/Light/Dark)

#### Layout Improvements
- Better card shadows and spacing
- Modern color palette with improved contrast
- Consistent border radius and spacing scale
- Improved typography hierarchy
- Better responsive behavior

## Implementation Plan

### Phase 1: Theme Infrastructure
1. Update CSS variables for complete light/dark theme support
2. Create ThemeProvider context and hooks
3. Add theme toggle functionality with localStorage persistence

### Phase 2: Account Settings
1. Create AccountSettings modal component
2. Move GitHub connector UI from Header to AccountSettings
3. Add profile and theme preference sections
4. Implement keyboard navigation and accessibility

### Phase 3: Visual Design Updates
1. Redesign Header with modern layout
2. Update main layout spacing and cards
3. Apply modern shadows and styling
4. Ensure responsive design works correctly

### Phase 4: Testing & Integration
1. Test theme switching and persistence
2. Verify all existing functionality still works
3. Test keyboard navigation and accessibility
4. Run local testing with npm run dev

## Design Tokens

### Colors (Enhanced)
```css
/* Light theme additions */
--bg-base: #f8fafc;
--bg-deep: #f1f5f9;
--bg-card: rgba(255, 255, 255, 0.92);

/* Dark theme (existing, refined) */
--bg-base: #0f172a;
--bg-deep: #1e293b;
--bg-card: rgba(15, 23, 42, 0.85);
```

### Spacing Scale
- xs: 4px
- sm: 8px  
- md: 16px
- lg: 24px
- xl: 32px

### Shadow System
- card: subtle elevation for content cards
- modal: stronger shadow for overlays
- glass: backdrop blur effects

## Accessibility Requirements
- All interactive elements keyboard accessible
- Theme toggle has proper ARIA labels
- Modal has proper focus management
- Color contrast meets WCAG AA standards
- Responsive design works on mobile devices

## Constraints & Considerations
- No backend/API changes allowed
- Must preserve all existing functionality
- Mobile responsive using existing Tailwind classes
- Maintain performance (no heavy animations)
- Single atomic deployment after local verification