import { SignInPage } from './pages/SignIn';
import { mount } from './shell';

// A separate entry: the sign-in page loads before there is a session, so its files are
// the only ones the server gives out without one. The console's own code stays private.
mount(<SignInPage />);
