using Microsoft.UI.Windowing;
using Microsoft.UI.Xaml;

namespace Rwhc.WinUI;

public sealed partial class MainWindow : Window
{
    public MainWindow()
    {
        InitializeComponent();
        AppWindow.Resize(new Windows.Graphics.SizeInt32(900, 640));
        RootFrame.Navigate(typeof(MainPage));
    }
}
