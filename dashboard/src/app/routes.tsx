import { createBrowserRouter } from "react-router";
import { DashboardLayout } from "./components/DashboardLayout";
import { Overview } from "./pages/Overview";
import { Trading } from "./pages/Trading";
import { Agents } from "./pages/Agents";
import { Analytics } from "./pages/Analytics";

const basename = import.meta.env.BASE_URL || '/app/';

export const router = createBrowserRouter([
  {
    path: "/",
    Component: DashboardLayout,
    children: [
      { index: true, Component: Overview },
      { path: "trading", Component: Trading },
      { path: "agents", Component: Agents },
      { path: "analytics", Component: Analytics },
    ],
  },
], { basename: basename.replace(/\/$/, '') });
